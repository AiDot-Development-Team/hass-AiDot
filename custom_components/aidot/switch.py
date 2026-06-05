"""Support for Aidot camera switches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator
from .entity import AidotEntity


@dataclass(frozen=True, kw_only=True)
class AidotSwitchDescription(SwitchEntityDescription):
    """Describes an Aidot camera switch."""

    get_is_on: Any = None        # callable(DeviceStatusData) -> bool | None
    async_turn_on_fn: Any = None  # async callable(DeviceClient) -> bool
    async_turn_off_fn: Any = None  # async callable(DeviceClient) -> bool


CAMERA_SWITCHES: tuple[AidotSwitchDescription, ...] = (
    AidotSwitchDescription(
        key="motion_detection",
        translation_key="motion_detection",
        icon="mdi:motion-sensor",
        get_is_on=lambda s: s.motion_detection,
        async_turn_on_fn=lambda c: c.async_set_motion_detection(True),
        async_turn_off_fn=lambda c: c.async_set_motion_detection(False),
    ),
    AidotSwitchDescription(
        key="status_led",
        translation_key="status_led",
        icon="mdi:led-on",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.status_led,
        async_turn_on_fn=lambda c: c.async_set_status_led(True),
        async_turn_off_fn=lambda c: c.async_set_status_led(False),
    ),
    AidotSwitchDescription(
        key="microphone",
        translation_key="microphone",
        icon="mdi:microphone",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.microphone,
        async_turn_on_fn=lambda c: c.async_set_microphone(True),
        async_turn_off_fn=lambda c: c.async_set_microphone(False),
    ),
    AidotSwitchDescription(
        key="floodlight",
        translation_key="floodlight",
        icon="mdi:light-flood-down",
        get_is_on=lambda s: s.floodlight,
        async_turn_on_fn=lambda c: c.async_set_floodlight(True),
        async_turn_off_fn=lambda c: c.async_set_floodlight(False),
    ),
    AidotSwitchDescription(
        key="ptz_tracking",
        translation_key="ptz_tracking",
        icon="mdi:radar",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.ptz_tracking,
        async_turn_on_fn=lambda c: c.async_set_ptz_tracking(True),
        async_turn_off_fn=lambda c: c.async_set_ptz_tracking(False),
    ),
    AidotSwitchDescription(
        key="ir_light",
        translation_key="ir_light",
        icon="mdi:led-off",
        entity_category=EntityCategory.CONFIG,
        get_is_on=lambda s: s.ir_light,
        async_turn_on_fn=lambda c: c.async_set_ir_light(True),
        async_turn_off_fn=lambda c: c.async_set_ir_light(False),
    ),
    AidotSwitchDescription(
        key="siren",
        translation_key="siren",
        icon="mdi:alarm-light",
        get_is_on=lambda s: s.siren,
        async_turn_on_fn=lambda c: c.async_set_siren(True),
        async_turn_off_fn=lambda c: c.async_set_siren(False),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Aidot camera switches."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_switches() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new = [
            AidotCameraSwitch(c, desc)
            for c in new_coords.values()
            for desc in CAMERA_SWITCHES
        ]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_switches()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_switches()))


class AidotCameraSwitch(AidotEntity, SwitchEntity):
    """A switch entity for an Aidot camera control."""

    entity_description: AidotSwitchDescription

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotSwitchDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_is_on(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_run_command(
            self.entity_description.async_turn_on_fn(self.device_client),
            f"turn on {self.name}",
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_run_command(
            self.entity_description.async_turn_off_fn(self.device_client),
            f"turn off {self.name}",
        )

