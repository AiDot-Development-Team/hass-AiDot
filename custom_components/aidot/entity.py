"""Base entity for Aidot devices - shared DeviceInfo + failure-surfacing commands."""

from __future__ import annotations

from collections.abc import Awaitable

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AidotDeviceUpdateCoordinator


def aidot_device_info(info) -> DeviceInfo:
    """Build the HA DeviceInfo for an Aidot device from its library info."""
    model_id = info.model_id or ""
    manufacturer = model_id.split(".")[0] if model_id else "AiDot"
    model = model_id[len(manufacturer) + 1:] if model_id else model_id
    mac = info.mac or ""
    return DeviceInfo(
        identifiers={(DOMAIN, info.dev_id)},
        connections={(CONNECTION_NETWORK_MAC, mac)} if mac else set(),
        manufacturer=manufacturer,
        model=model,
        name=info.name,
        hw_version=info.hw_version,
    )


class AidotEntity(CoordinatorEntity[AidotDeviceUpdateCoordinator]):
    """Common base: builds DeviceInfo once and runs library commands safely."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: AidotDeviceUpdateCoordinator, key: str | None = None
    ) -> None:
        super().__init__(coordinator)
        info = coordinator.device_client.info
        if key is not None:
            self._attr_unique_id = f"{info.dev_id}_{key}"
        self._attr_device_info = aidot_device_info(info)

    @property
    def device_client(self):
        """The underlying aidot DeviceClient for this entity."""
        return self.coordinator.device_client

    async def async_run_command(self, coro: Awaitable, action: str) -> None:
        """Await a library command, surfacing failures to the user.

        Library setters return ``False`` when the device rejects the change and
        may raise on network/auth errors; both become a ``HomeAssistantError``
        (which HA shows to the user) instead of an optimistic silent success.
        On success the entity state is written immediately.
        """
        try:
            ok = await coro
        except HomeAssistantError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface any library/network error
            raise HomeAssistantError(f"AiDot {action} failed: {exc}") from exc
        if ok is False:
            raise HomeAssistantError(f"AiDot {action} was rejected by the device")
        self.async_write_ha_state()
