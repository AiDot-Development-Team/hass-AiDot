"""Coordinator for Aidot."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import override

from aiohttp import ClientResponseError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from aidot.client import AidotClient
from aidot.const import (
    CONF_ACCESS_TOKEN,
    CONF_AES_KEY,
    CONF_DEVICE_LIST,
    CONF_ID,
    CONF_TYPE,
)
from aidot.device_client import DeviceClient, DeviceStatusData
from aidot.exceptions import AidotAuthFailed, AidotUserOrPassIncorrect

from .auth import (
    async_get_devices_with_relogin,
    get_login_data,
    is_authentication_error,
)
from .ble_gateway_client import (
    BleGatewayDeviceClient,
    close_all_hub_sessions,
    start_all_hub_sessions,
)
from .const import DOMAIN
from .group_cloud import GroupCloudClient
from .group_device import GroupDeviceClient
from .group_protocol import GroupProtocolError, build_group_request, group_response_code

type AidotConfigEntry = ConfigEntry[AidotDeviceManagerCoordinator]
_LOGGER = logging.getLogger(__name__)

UPDATE_DEVICE_LIST_INTERVAL = timedelta(hours=6)
HUB_RECOVERY_REFRESH_INTERVAL = 300

_HUB_TYPE = "BleMesh_Hub"


class AidotDeviceUpdateCoordinator(DataUpdateCoordinator[DeviceStatusData]):
    """Class to manage Aidot data."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AidotConfigEntry,
        device_client: DeviceClient,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=None,
        )
        self.device_client = device_client

    @override
    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        self.device_client.on_status_update = self._handle_status_update

    def _handle_status_update(self, status: DeviceStatusData) -> None:
        """Handle status callback."""
        self.async_set_updated_data(status)

    @override
    async def _async_update_data(self) -> DeviceStatusData:
        """Return current status."""
        return self.device_client.status


class AidotDeviceManagerCoordinator(DataUpdateCoordinator[None]):
    """Class to manage fetching Aidot data."""

    config_entry: AidotConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AidotConfigEntry,
    ) -> None:
        """Initialize coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_DEVICE_LIST_INTERVAL,
        )
        self.client = AidotClient(
            session=async_get_clientsession(hass),
            token=get_login_data(config_entry.data),
        )
        self.client.set_token_fresh_cb(self.token_fresh_cb)
        self.device_coordinators: dict[str, AidotDeviceUpdateCoordinator] = {}
        self.group_coordinators: dict[str, AidotDeviceUpdateCoordinator] = {}
        self._cloud_login_lock = asyncio.Lock()
        self._group_retry_task: asyncio.Task | None = None
        self._group_discovery_valid = False
        self.group_status = {
            "eligibleCount": 0,
            "excludedCount": 0,
            "lastDiscoveryErrorType": None,
        }
        self.group_cloud = GroupCloudClient(
            async_get_clientsession(hass),
            lambda: self.client.login_info,
            self._async_recover_group_auth,
            on_message=self._handle_group_cloud_message,
            on_connection=self._handle_group_connection,
        )
        self._last_hub_refresh = time.monotonic()
        self.cloud_status = {
            "reauthenticationCount": 0,
            "lastReauthentication": None,
            "lastSuccessfulRefresh": None,
        }

    @override
    async def _async_setup(self) -> None:
        """Set up the coordinator."""
        try:
            await self.async_auto_login()
        except AidotUserOrPassIncorrect as error:
            raise ConfigEntryAuthFailed from error
        except ClientResponseError as error:
            if is_authentication_error(error):
                raise ConfigEntryAuthFailed from error
            raise

    @override
    async def _async_update_data(self) -> None:
        """Update data async."""
        try:
            async with self._cloud_login_lock:
                data = await async_get_devices_with_relogin(
                    self.client, self._handle_cloud_relogin
                )
        except (AidotAuthFailed, AidotUserOrPassIncorrect) as error:
            raise ConfigEntryAuthFailed from error
        except ClientResponseError as error:
            if is_authentication_error(error):
                raise ConfigEntryAuthFailed from error
            raise
        self._last_hub_refresh = time.monotonic()
        self.cloud_status["lastSuccessfulRefresh"] = datetime.now(UTC).isoformat()

        all_devices: list[dict] = data[CONF_DEVICE_LIST]

        hub_devices = {
            d[CONF_ID]: d for d in all_devices if d.get(CONF_TYPE) == _HUB_TYPE
        }

        wifi_devices = {
            d[CONF_ID]: d
            for d in all_devices
            if (
                d.get(CONF_TYPE) == "light"
                and CONF_AES_KEY in d
                and d[CONF_AES_KEY][0] is not None
            )
        }

        ble_devices = {
            d[CONF_ID]: d
            for d in all_devices
            if (
                d.get(CONF_TYPE) == "light"
                and d.get("directGateway") in hub_devices
                and d.get("bleMeshDeviceKey") is not None
            )
        }

        current_devices = {**wifi_devices, **ble_devices}

        removed_ids = set(self.device_coordinators) - set(current_devices)
        for dev_id in removed_ids:
            coordinator = self.device_coordinators.pop(dev_id)
            coordinator.device_client.on_status_update = None
            if isinstance(coordinator.device_client, BleGatewayDeviceClient):
                await coordinator.device_client.async_stop()
        if removed_ids:
            self._purge_deleted_lists()

        login_data = get_login_data(self.config_entry.data)
        user_id: str = login_data.get("id", "")

        for dev_id, device in current_devices.items():
            if dev_id not in self.device_coordinators:
                if dev_id in ble_devices:
                    hub_device = hub_devices[device["directGateway"]]
                    device_client: DeviceClient | BleGatewayDeviceClient = (
                        BleGatewayDeviceClient(device, hub_device, user_id)
                    )
                    await device_client.async_update_hub(
                        hub_device, user_id, self._schedule_hub_refresh
                    )
                    _LOGGER.debug(
                        "Using BLE gateway client for %s via hub %s",
                        device.get("name"),
                        hub_device.get("name"),
                    )
                else:
                    device_client = self.client.get_device_client(device)

                device_coordinator = AidotDeviceUpdateCoordinator(
                    self.hass, self.config_entry, device_client
                )
                await device_coordinator.async_config_entry_first_refresh()
                self.device_coordinators[dev_id] = device_coordinator
            elif dev_id in ble_devices:
                existing_client = self.device_coordinators[dev_id].device_client
                if isinstance(existing_client, BleGatewayDeviceClient):
                    await existing_client.async_update_hub(
                        hub_devices[device["directGateway"]],
                        user_id,
                        self._schedule_hub_refresh,
                    )
                    existing_client.update_cloud_properties(device)

        await start_all_hub_sessions()
        try:
            await self._async_refresh_groups(all_devices)
        except Exception as error:  # noqa: BLE001 - optional groups must not disable local lights
            self._group_discovery_valid = False
            self.group_status["lastDiscoveryErrorType"] = type(error).__name__
            for coordinator in self.group_coordinators.values():
                coordinator.device_client.invalidate()
            self.group_cloud.set_hubs(set())
            _LOGGER.debug("AiDot group discovery failed: %s", type(error).__name__)
            if self._group_retry_task is None:
                self._group_retry_task = self.hass.async_create_task(
                    self._async_retry_groups()
                )

    async def _async_refresh_groups(self, devices: list[dict]) -> None:
        targets, excluded = await self.group_cloud.discover(devices)
        current: set[str] = set()
        for home_id, group in targets:
            members = [
                self.device_coordinators[member].device_client
                for member in group["deviceIds"]
            ]
            if not all(
                isinstance(member, BleGatewayDeviceClient) for member in members
            ):
                continue
            unique_id = f"group:{home_id}:{group['id']}"
            current.add(unique_id)
            if unique_id in self.group_coordinators:
                self.group_coordinators[unique_id].device_client.update(
                    home_id, group, devices, members
                )
            else:
                client = GroupDeviceClient(
                    home_id, group, devices, members, self.group_cloud
                )
                coordinator = AidotDeviceUpdateCoordinator(
                    self.hass, self.config_entry, client
                )
                await coordinator.async_config_entry_first_refresh()
                self.group_coordinators[unique_id] = coordinator
        for unique_id in set(self.group_coordinators) - current:
            # Keep the loaded coordinator so a removed/re-added app group can
            # reuse the same HA entity. A reload purges obsolete group devices.
            self.group_coordinators[unique_id].device_client.invalidate()
        self._group_discovery_valid = True
        self.group_status.update(
            {
                "eligibleCount": len(current),
                "excludedCount": excluded,
                "lastDiscoveryErrorType": None,
            }
        )
        self.group_cloud.set_hubs(
            {
                c.device_client.hub_id
                for c in self.group_coordinators.values()
                if c.device_client.valid
            }
        )
        if current:
            self.group_cloud.start()
        self._purge_deleted_lists()

    async def _async_retry_groups(self) -> None:
        try:
            while not self._group_discovery_valid:
                await asyncio.sleep(60)
                await self.async_request_refresh()
        finally:
            self._group_retry_task = None

    async def _async_recover_group_auth(self, rejected_token: str) -> None:
        async with self._cloud_login_lock:
            if self.client.login_info.get("accessToken") != rejected_token:
                return
            try:
                await self.client.async_post_login()
            except (AidotAuthFailed, AidotUserOrPassIncorrect):
                self.config_entry.async_start_reauth(self.hass)
                raise
            self._handle_cloud_relogin()

    def _handle_group_connection(self, connected: bool) -> None:
        for coordinator in self.group_coordinators.values():
            coordinator.device_client.set_connected(connected)

    def _handle_group_cloud_message(self, topic: str, message: dict) -> None:
        """Route actual member notifications and app group acknowledgements."""
        payload = message.get("payload")
        if not isinstance(payload, dict):
            return
        member_id = payload.get("devId")
        if isinstance(member_id, str) and isinstance(payload.get("attr"), dict):
            coordinator = self.device_coordinators.get(member_id)
            client = coordinator.device_client if coordinator is not None else None
            if (
                isinstance(client, BleGatewayDeviceClient)
                and topic == f"iot/v1/cb/{client.hub_id}/device/setDevAttrNotif"
            ):
                client.handle_cloud_notification(payload["attr"])
            return
        user_id = self.client.login_info.get("id", "")
        if topic != f"iot/v1/c/{user_id}/group/excGroupResp":
            return
        seq = message.get("seq")
        if not isinstance(seq, str):
            return
        for coordinator in self.group_coordinators.values():
            client = coordinator.device_client
            if not client.valid:
                continue
            if (
                payload.get("groupId") != client.group["id"]
                or payload.get("homeId") != client.home_id
            ):
                continue
            params = payload.get("params")
            if not isinstance(params, dict):
                return
            attributes = {
                key: value for key, value in params.items() if key in client.attributes
            }
            try:
                _, request = build_group_request(
                    group=client.group,
                    devices=client.devices,
                    home_id=client.home_id,
                    user_id=user_id,
                    client_id=f"observed-{user_id}",
                    attributes=attributes,
                    sequence=seq,
                    unix_time=0,
                )
                if (
                    group_response_code(
                        topic=topic, message=message, request=request, user_id=user_id
                    )
                    == 200
                ):
                    client.apply_accepted_attributes(attributes)
            except GroupProtocolError:
                pass
            return

    def _schedule_hub_refresh(self) -> None:
        """Refresh expired hub connection data without hammering the cloud."""
        now = time.monotonic()
        if now - self._last_hub_refresh < HUB_RECOVERY_REFRESH_INTERVAL:
            return
        self._last_hub_refresh = now
        self.hass.async_create_task(self.async_request_refresh())

    async def async_cleanup(self) -> None:
        """Perform cleanup actions."""
        if self._group_retry_task is not None:
            task = self._group_retry_task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.group_cloud.close()
        for coordinator in self.group_coordinators.values():
            coordinator.device_client.on_status_update = None
        for coordinator in self.device_coordinators.values():
            coordinator.device_client.on_status_update = None
            if isinstance(coordinator.device_client, BleGatewayDeviceClient):
                await coordinator.device_client.async_stop()
        await close_all_hub_sessions()
        await self.client.async_cleanup()

    def token_fresh_cb(self) -> None:
        """Update token."""
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={
                **get_login_data(self.config_entry.data),
                **self.client.login_info,
            },
        )
        self.group_cloud.token_updated()

    def _handle_cloud_relogin(self) -> None:
        """Save a recovered session and make successful recovery observable."""
        self.token_fresh_cb()
        self.cloud_status["reauthenticationCount"] += 1
        self.cloud_status["lastReauthentication"] = datetime.now(UTC).isoformat()

    async def async_auto_login(self) -> None:
        """Async auto login."""
        if self.client.login_info.get(CONF_ACCESS_TOKEN) is None:
            await self.client.async_post_login()
            self.token_fresh_cb()

    def _purge_deleted_lists(self) -> None:
        """Purge device entries of deleted lists."""
        device_reg = dr.async_get(self.hass)
        identifiers = {
            (
                DOMAIN,
                device_coordinator.device_client.info.dev_id,
            )
            for device_coordinator in self.device_coordinators.values()
        }
        identifiers.update(
            (DOMAIN, c.device_client.info.dev_id)
            for c in self.group_coordinators.values()
        )
        for device in dr.async_entries_for_config_entry(
            device_reg, self.config_entry.entry_id
        ):
            if not set(device.identifiers) & identifiers:
                _LOGGER.debug("Removing obsolete device entry %s", device.name)
                device_reg.async_update_device(
                    device.id, remove_config_entry_id=self.config_entry.entry_id
                )
