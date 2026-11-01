"""Support for Aidot lights."""

import ctypes
from typing import Any, override

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
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
    async_add_entities(
        AidotLight(device_coordinator)
        for device_coordinator in coordinator.device_coordinators.values()
    )
    known_groups: set[str] = set()

    @callback
    def add_groups() -> None:
        entities = []
        for unique_id, group_coordinator in coordinator.group_coordinators.items():
            if unique_id not in known_groups:
                known_groups.add(unique_id)
                entities.append(AidotGroupLight(group_coordinator))
        if entities:
            async_add_entities(entities)

    add_groups()
    entry.async_on_unload(coordinator.async_add_listener(add_groups))


class AidotLight(CoordinatorEntity[AidotDeviceUpdateCoordinator], LightEntity):
    """Representation of a Aidot Wi-Fi Light."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: AidotDeviceUpdateCoordinator) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._attr_unique_id = coordinator.device_client.info.dev_id
        if hasattr(coordinator.device_client.info, "cct_max"):
            self._attr_max_color_temp_kelvin = coordinator.device_client.info.cct_max
        if hasattr(coordinator.device_client.info, "cct_min"):
            self._attr_min_color_temp_kelvin = coordinator.device_client.info.cct_min

        model_id = coordinator.device_client.info.model_id
        manufacturer = model_id.split(".")[0]
        model = model_id[len(manufacturer) + 1 :]
        mac = coordinator.device_client.info.mac

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            connections={(CONNECTION_NETWORK_MAC, mac)},
            manufacturer=manufacturer,
            model=model,
            name=coordinator.device_client.info.name,
            hw_version=coordinator.device_client.info.hw_version,
        )
        self._effect_codes = getattr(coordinator.device_client.info, "effects", {})
        self._effect_names = {code: name for name, code in self._effect_codes.items()}
        self._preset_names = getattr(coordinator.device_client.info, "preset_names", [])
        effects = list(dict.fromkeys([*self._effect_codes, *self._preset_names]))
        if effects:
            self._attr_effect_list = effects
            self._attr_supported_features = LightEntityFeature.EFFECT
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
        """Update light status from coordinator data."""
        self._attr_is_on = self.coordinator.data.on
        self._attr_brightness = self.coordinator.data.dimming
        self._attr_color_temp_kelvin = self.coordinator.data.cct
        self._attr_rgbw_color = self.coordinator.data.rgbw
        self._attr_effect = self._effect_names.get(
            getattr(self.coordinator.data, "effect_mode", None)
        ) or getattr(self.coordinator.data, "effect", None) or None
        active_mode = getattr(self.coordinator.data, "active_color_mode", None)
        if active_mode in self._attr_supported_color_modes:
            self._attr_color_mode = ColorMode(active_mode)

    @property
    @override
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self.coordinator.data.online

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        """Update."""
        self._update_status()
        super()._handle_coordinator_update()

    @override
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Batch light attributes, then delegate Wi-Fi presets to the library."""
        attributes: dict[str, int] = {"OnOff": 1}
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            attributes["CCT"] = int(kwargs[ATTR_COLOR_TEMP_KELVIN])
        if ATTR_RGBW_COLOR in kwargs:
            rgbw = tuple(max(0, min(255, round(c))) for c in kwargs[ATTR_RGBW_COLOR])
            packed = (rgbw[0] << 24) | (rgbw[1] << 16) | (rgbw[2] << 8) | rgbw[3]
            attributes["RGBW"] = ctypes.c_int32(packed).value
        preset = kwargs.get(ATTR_EFFECT)
        if preset not in self._preset_names:
            preset = None
        if ATTR_EFFECT in kwargs and preset is None:
            attributes["EffectMode"] = self._effect_codes[kwargs[ATTR_EFFECT]]
        if ATTR_BRIGHTNESS in kwargs:
            attributes["Dimming"] = int(kwargs[ATTR_BRIGHTNESS] * 100 / 255)

        # Both the Wi-Fi library and BLE adapter accept a complete attribute map.
        # Do not send cached colour alongside brightness-only or plain-on calls.
        await self.coordinator.device_client.send_dev_attr(attributes)
        if preset is not None:
            # Wi-Fi presets use the upstream cloud primitive API, not a numeric
            # BLE EffectMode. Keep that path intact when applying brightness.
            await self.coordinator.device_client.async_set_effect(preset)
        status = self.coordinator.data
        status.on = True
        if "CCT" in attributes:
            status.cct = attributes["CCT"]
            status.active_color_mode = ColorMode.COLOR_TEMP
        if "RGBW" in attributes:
            status.rgbw = rgbw
            status.active_color_mode = ColorMode.RGBW
        if "Dimming" in attributes:
            status.dimming = int(attributes["Dimming"] * 255 / 100)
        if ATTR_EFFECT not in kwargs and ("CCT" in attributes or "RGBW" in attributes):
            if hasattr(status, "effect"):
                status.effect = ""
            if hasattr(status, "effect_mode"):
                status.effect_mode = None
        if "EffectMode" in attributes:
            status.effect_mode = attributes["EffectMode"]
        if preset is not None:
            status.effect = preset
        self._update_status()
        self.async_write_ha_state()

    @override
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self.coordinator.device_client.async_turn_off()
        self.coordinator.data.on = False
        self._attr_is_on = False
        self.async_write_ha_state()


class AidotGroupLight(AidotLight):
    """An existing app group, controlled through AiDot's cloud."""

    _attr_assumed_state = True

    def _update_status(self) -> None:
        info = self.coordinator.device_client.info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, info.dev_id)}, manufacturer="AiDot",
            model="Mesh group", name=info.name,
        )
        self._effect_codes = info.effects
        self._effect_names = {code: name for name, code in info.effects.items()}
        self._attr_effect_list = list(info.effects) or None
        self._attr_supported_features = LightEntityFeature.EFFECT if info.effects else 0
        if info.enable_rgbw:
            self._attr_supported_color_modes = {ColorMode.RGBW}
            if info.enable_cct:
                self._attr_supported_color_modes.add(ColorMode.COLOR_TEMP)
            self._attr_color_mode = ColorMode.RGBW
        elif info.enable_cct:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif info.enable_dimming:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF
        self._attr_min_color_temp_kelvin = info.cct_min
        self._attr_max_color_temp_kelvin = info.cct_max
        super()._update_status()

    @property
    def extra_state_attributes(self) -> dict:
        return {"member_count": self.coordinator.device_client.report["memberCount"]}
