"""Support for Aidot lights."""

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGBW_COLOR,
    DEFAULT_MAX_KELVIN,
    DEFAULT_MIN_KELVIN,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Light."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_lights() -> None:
        new = [
            AidotLight(c)
            for dev_id, c in coordinator.device_coordinators.items()
            if dev_id not in registered
        ]
        if new:
            registered.update(c.unique_id for c in new)
            async_add_entities(new)

    _add_new_lights()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_lights()))


class AidotLight(CoordinatorEntity[AidotDeviceUpdateCoordinator], LightEntity):
    """Representation of an Aidot Wi-Fi Light."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: AidotDeviceUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.device_client.info.dev_id
        # Always set kelvin bounds (with HA defaults) so color-temp lights never
        # fall back to the deprecated mireds properties - RGBW lights enable
        # COLOR_TEMP without a CCT service that provides cct_min/cct_max.
        self._attr_max_color_temp_kelvin = (
            getattr(coordinator.device_client.info, "cct_max", None) or DEFAULT_MAX_KELVIN
        )
        self._attr_min_color_temp_kelvin = (
            getattr(coordinator.device_client.info, "cct_min", None) or DEFAULT_MIN_KELVIN
        )

        model_id = coordinator.device_client.info.model_id
        manufacturer = model_id.split(".")[0]
        model = model_id[len(manufacturer) + 1:]
        mac = coordinator.device_client.info.mac

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            connections={(CONNECTION_NETWORK_MAC, mac)},
            manufacturer=manufacturer,
            model=model,
            name=coordinator.device_client.info.name,
            hw_version=coordinator.device_client.info.hw_version,
        )
        if coordinator.device_client.info.enable_rgbw:
            self._attr_color_mode = ColorMode.RGBW
            self._attr_supported_color_modes = {ColorMode.RGBW, ColorMode.COLOR_TEMP}
        elif coordinator.device_client.info.enable_cct:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        else:
            self._attr_color_mode = ColorMode.BRIGHTNESS
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._update_status()

    def _update_status(self) -> None:
        if self.coordinator.data is None:
            return
        self._attr_is_on = self.coordinator.data.on
        self._attr_brightness = self.coordinator.data.dimming
        self._attr_color_temp_kelvin = self.coordinator.data.cct
        self._attr_rgbw_color = self.coordinator.data.rgbw

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self.coordinator.data.online
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_status()
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            await self.coordinator.device_client.async_set_brightness(brightness)
            self.coordinator.data.dimming = brightness
            self._attr_brightness = brightness
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            color_temp_kelvin = kwargs[ATTR_COLOR_TEMP_KELVIN]
            await self.coordinator.device_client.async_set_cct(color_temp_kelvin)
            self.coordinator.data.cct = color_temp_kelvin
            self._attr_color_temp_kelvin = color_temp_kelvin
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ATTR_RGBW_COLOR in kwargs:
            rgbw_color = kwargs[ATTR_RGBW_COLOR]
            await self.coordinator.device_client.async_set_rgbw(rgbw_color)
            self.coordinator.data.rgbw = rgbw_color
            self._attr_rgbw_color = rgbw_color
            self._attr_color_mode = ColorMode.RGBW
        else:
            await self.coordinator.device_client.async_turn_on()

        self.coordinator.data.on = True
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.device_client.async_turn_off()
        self.coordinator.data.on = False
        self._attr_is_on = False
        self.async_write_ha_state()
