"""Coordinator for Aidot."""

import asyncio
from datetime import timedelta
import logging

from aidot.client import AidotClient
from aidot.const import (
    CONF_ACCESS_TOKEN,
    CONF_AES_KEY,
    CONF_DEVICE_LIST,
    CONF_ID,
    CONF_PRODUCT,
    CONF_SERVICE_MODULES,
    CONF_IDENTITY,
    CONF_MODEL_ID,
)
from aidot.device_client import DeviceClient, DeviceStatusData
from aidot.exceptions import AidotAuthFailed, AidotUserOrPassIncorrect

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

type AidotConfigEntry = ConfigEntry[AidotDeviceManagerCoordinator]
_LOGGER = logging.getLogger(__name__)

UPDATE_DEVICE_LIST_INTERVAL = timedelta(hours=6)
UPDATE_CAMERA_ATTRS_INTERVAL = timedelta(minutes=5)

_CONF_TYPE = "type"


def _is_camera_device(device: dict) -> bool:
    """Return True if the device is a camera (IPC model or camera service module)."""
    model = (device.get(CONF_MODEL_ID) or "").upper()
    if "IPC" in model:
        return True
    product = device.get(CONF_PRODUCT) or {}
    for module in product.get(CONF_SERVICE_MODULES) or []:
        ident = (module.get(CONF_IDENTITY) or "").lower()
        if "camera" in ident or "ipc" in ident:
            return True
    return False


def _is_light_device(device: dict) -> bool:
    """Return True if the device is a light (has aesKey and type=light)."""
    return (
        device.get(_CONF_TYPE) == "light"
        and CONF_AES_KEY in device
        and device[CONF_AES_KEY][0] is not None
    )


class AidotDeviceUpdateCoordinator(DataUpdateCoordinator[DeviceStatusData]):
    """Manage data for a single Aidot light device (TCP push updates)."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AidotConfigEntry,
        device_client: DeviceClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=None,
        )
        self.device_client = device_client

    async def _async_setup(self) -> None:
        self.device_client.set_status_fresh_cb(self._handle_status_update)

    def _handle_status_update(self, status: DeviceStatusData) -> None:
        self.async_set_updated_data(status)

    async def _async_update_data(self) -> DeviceStatusData:
        return self.device_client.status


class AidotCameraUpdateCoordinator(AidotDeviceUpdateCoordinator):
    """Manage data for a single Aidot camera device (MQTT polled attributes)."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AidotConfigEntry,
        device_client: DeviceClient,
        manager: "AidotDeviceManagerCoordinator",
    ) -> None:
        super().__init__(hass, config_entry, device_client)
        self.update_interval = UPDATE_CAMERA_ATTRS_INTERVAL
        self._manager = manager
        self._motion_listeners: list = []

    def add_motion_listener(self, cb) -> callable:
        """Register a callback fired for each new motion/person cloud event.

        Returns a function that removes the listener.
        """
        self._motion_listeners.append(cb)

        def _remove() -> None:
            if cb in self._motion_listeners:
                self._motion_listeners.remove(cb)

        return _remove

    @callback
    def _handle_motion_event(self, event: dict) -> None:
        # Isolate listeners: one raising callback must not drop the event for the
        # others (event entity + occupancy sensor both subscribe) or propagate
        # into the library's motion-poll task.
        for cb in list(self._motion_listeners):
            try:
                cb(event)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Aidot motion listener raised")

    async def _async_setup(self) -> None:
        # Camera devices don't push status via TCP - skip set_status_fresh_cb.
        # Streaming is lazy for all models: the camera entity's stream_source()
        # starts the HTTP-listen serve (go2rtc pulls it) only when a viewer
        # connects, so we don't hold a WebRTC session / decode open 24/7 (Pi
        # friendly). Here we only start cloud motion-event polling.
        await self.device_client.async_start_motion_polling(self._handle_motion_event)

    async def _async_update_data(self) -> DeviceStatusData:
        # Refresh sensors + control-entity states from the cloud device payload
        # (battery, SD-card, occupancy, motion/night-vision, …).  This is the
        # reliable source the official app reads; cameras don't push these over
        # MQTT, so we no longer spin up a per-camera MQTT attribute poll.
        try:
            device = await self._manager.async_get_camera_device(
                self.device_client.device_id
            )
            if device:
                self.device_client.update_status_from_device(device)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Camera status refresh failed for %s (will retry): %s",
                self.device_client.device_id, exc,
            )
        return self.device_client.status


class AidotDeviceManagerCoordinator(DataUpdateCoordinator[None]):
    """Manage the full AiDot device list and spawn per-device coordinators."""

    config_entry: AidotConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AidotConfigEntry,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_DEVICE_LIST_INTERVAL,
        )
        self.client = AidotClient(
            session=async_get_clientsession(hass),
            token=config_entry.data,
        )
        self.client.set_token_fresh_cb(self.token_fresh_cb)
        self.device_coordinators: dict[str, AidotDeviceUpdateCoordinator] = {}
        self.camera_coordinators: dict[str, AidotCameraUpdateCoordinator] = {}
        # Short-TTL cache of the device list, so the per-camera attribute polls
        # (every 5 min, up to one per camera) share a single cloud fetch instead
        # of each re-pulling the whole list.
        self._dev_cache: dict[str, dict] = {}
        self._dev_cache_ts: float = 0.0
        self._dev_fetch_lock = asyncio.Lock()

    async def _async_setup(self) -> None:
        try:
            await self.async_auto_login()
        except AidotUserOrPassIncorrect as error:
            raise ConfigEntryAuthFailed from error

    async def _async_update_data(self) -> None:
        try:
            data = await self.client.async_get_all_device()
        except AidotAuthFailed as error:
            # Access token AND refresh token expired (e.g. the integration was
            # disabled for a while). Try a headless full re-login with the stored
            # credentials before surfacing a reauth prompt to the user.
            _ensure = getattr(self.client, "async_ensure_token", None)
            if _ensure is None or not await _ensure():
                raise ConfigEntryAuthFailed from error
            try:
                data = await self.client.async_get_all_device()
            except AidotAuthFailed as error2:
                raise ConfigEntryAuthFailed from error2

        all_devices = data[CONF_DEVICE_LIST]

        current_lights = {
            d[CONF_ID]: d for d in all_devices if _is_light_device(d)
        }
        self._sync_light_coordinators(current_lights)

        current_cameras = {
            d[CONF_ID]: d for d in all_devices if _is_camera_device(d)
        }
        self._sync_camera_coordinators(current_cameras)

        # Refresh camera sensors / control-entity states from the just-fetched
        # cloud "properties" (battery, SD-card, occupancy, motion, night-vision,
        # …) - the reliable source the app reads; cameras don't push these over
        # MQTT.  Also seeds the short-TTL cache the per-camera polls reuse.
        self._dev_cache = current_cameras
        self._dev_cache_ts = self.hass.loop.time()
        for dev_id, device in current_cameras.items():
            coord = self.camera_coordinators.get(dev_id)
            if coord is not None:
                coord.device_client.update_status_from_device(device)

    async def async_get_camera_device(self, device_id: str) -> dict | None:
        """Return a camera's current cloud device dict (60s-cached list fetch).

        Shared by the per-camera coordinators so they don't each re-pull the
        full device list every 5 minutes.
        """
        now = self.hass.loop.time()
        async with self._dev_fetch_lock:
            if not self._dev_cache or (now - self._dev_cache_ts) > 60:
                data = await self.client.async_get_all_device()
                self._dev_cache = {
                    d[CONF_ID]: d
                    for d in data[CONF_DEVICE_LIST]
                    if _is_camera_device(d)
                }
                self._dev_cache_ts = now
        return self._dev_cache.get(device_id)

    def _sync_light_coordinators(self, current: dict[str, dict]) -> None:
        self._sync_coordinators(self.device_coordinators, current, is_camera=False)

    def _sync_camera_coordinators(self, current: dict[str, dict]) -> None:
        self._sync_coordinators(self.camera_coordinators, current, is_camera=True)

    def _sync_coordinators(
        self,
        coord_dict: dict[str, AidotDeviceUpdateCoordinator],
        current: dict[str, dict],
        *,
        is_camera: bool,
    ) -> None:
        removed = set(coord_dict) - set(current)
        for dev_id in removed:
            coord = coord_dict.pop(dev_id)
            coord.device_client.set_status_fresh_cb(None)
            if is_camera:
                self.hass.async_create_task(
                    coord.device_client.async_stop_streaming()
                )
                self.hass.async_create_task(
                    coord.device_client.async_stop_motion_polling()
                )
        if removed:
            self._purge_deleted_entries()
        for dev_id, device in current.items():
            if dev_id not in coord_dict:
                dc = self.client.get_device_client(device)
                coord: AidotDeviceUpdateCoordinator
                if is_camera:
                    coord = AidotCameraUpdateCoordinator(
                        self.hass, self.config_entry, dc, self
                    )
                else:
                    coord = AidotDeviceUpdateCoordinator(
                        self.hass, self.config_entry, dc
                    )
                self.hass.async_create_task(
                    self._async_init_coordinator(coord, is_camera=is_camera)
                )
                coord_dict[dev_id] = coord

    async def _async_init_coordinator(
        self, coord: AidotDeviceUpdateCoordinator, *, is_camera: bool
    ) -> None:
        """Bring a per-device coordinator up, at setup or at runtime.

        ``async_config_entry_first_refresh`` may only run while the entry is
        SETUP_IN_PROGRESS; on the periodic device-list refresh (entry LOADED) it
        raises. A device added to the account after setup is discovered there, so
        for that path we run the setup hook (which starts camera motion polling -
        ``async_refresh`` skips it) and a plain refresh. Wrapped so this
        fire-and-forget task never surfaces an unhandled exception.
        """
        try:
            if self.config_entry.state is ConfigEntryState.SETUP_IN_PROGRESS:
                await coord.async_config_entry_first_refresh()
            else:
                await coord._async_setup()
                await coord.async_refresh()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "Aidot: failed to initialise coordinator for %s: %s",
                coord.device_client.device_id, exc,
            )

    async def async_cleanup(self) -> None:
        for coord in self.device_coordinators.values():
            coord.device_client.set_status_fresh_cb(None)
        for coord in self.camera_coordinators.values():
            await coord.device_client.async_stop_motion_polling()
            await coord.device_client.async_stop_streaming()
        await self.client.async_cleanup()

    def token_fresh_cb(self) -> None:
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=self.client.login_info.copy()
        )

    async def async_auto_login(self) -> None:
        if self.client.login_info.get(CONF_ACCESS_TOKEN) is None:
            await self.client.async_post_login()

    def _purge_deleted_entries(self) -> None:
        device_reg = dr.async_get(self.hass)
        all_ids = {
            (DOMAIN, c.device_client.info.dev_id)
            for c in list(self.device_coordinators.values())
            + list(self.camera_coordinators.values())
        }
        for device in dr.async_entries_for_config_entry(
            device_reg, self.config_entry.entry_id
        ):
            if not set(device.identifiers) & all_ids:
                _LOGGER.debug("Removing obsolete device entry %s", device.name)
                device_reg.async_update_device(
                    device.id, remove_config_entry_id=self.config_entry.entry_id
                )
