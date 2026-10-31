"""Coordinator for Aidot."""

from datetime import UTC, datetime, timedelta
import logging
import time
from typing import override

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
from aiohttp import ClientResponseError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .auth import async_get_devices_with_relogin, get_login_data, is_authentication_error
from .ble_gateway_client import (
    BleGatewayDeviceClient,
    close_all_hub_sessions,
    start_all_hub_sessions,
)
from .const import DOMAIN

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
            d[CONF_ID]: d
            for d in all_devices
            if d.get(CONF_TYPE) == _HUB_TYPE
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

    def _schedule_hub_refresh(self) -> None:
        """Refresh expired hub connection data without hammering the cloud."""
        now = time.monotonic()
        if now - self._last_hub_refresh < HUB_RECOVERY_REFRESH_INTERVAL:
            return
        self._last_hub_refresh = now
        self.hass.async_create_task(self.async_request_refresh())

    async def async_cleanup(self) -> None:
        """Perform cleanup actions."""
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
        for device in dr.async_entries_for_config_entry(
            device_reg, self.config_entry.entry_id
        ):
            if not set(device.identifiers) & identifiers:
                _LOGGER.debug("Removing obsolete device entry %s", device.name)
                device_reg.async_update_device(
                    device.id, remove_config_entry_id=self.config_entry.entry_id
                )
