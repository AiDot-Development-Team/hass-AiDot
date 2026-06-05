"""Number entities for Aidot cameras (e.g. motion detection sensitivity)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import AidotEntity


@dataclass(frozen=True, kw_only=True)
class AidotNumberDescription(NumberEntityDescription):
    """Describes an Aidot camera number entity."""

    get_value: Any = None          # callable(DeviceStatusData) -> float | None
    async_set_fn: Any = None       # async callable(DeviceClient, float) -> bool


CAMERA_NUMBERS: tuple[AidotNumberDescription, ...] = (
    AidotNumberDescription(
        key="motion_sensitivity",
        translation_key="motion_sensitivity",
        icon="mdi:motion-sensor",
        entity_category=EntityCategory.CONFIG,
        native_min_value=1,
        native_max_value=5,
        native_step=1,
        mode=NumberMode.SLIDER,
        get_value=lambda s: s.motion_sensitivity,
        async_set_fn=lambda c, v: c.async_set_motion_sensitivity(int(v)),
    ),
    AidotNumberDescription(
        key="speaker_volume",
        translation_key="speaker_volume",
        icon="mdi:volume-high",
        entity_category=EntityCategory.CONFIG,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.SLIDER,
        get_value=lambda s: s.speaker_volume,
        async_set_fn=lambda c, v: c.async_set_speaker_volume(int(v)),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Aidot camera number entities."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_numbers() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new = [
            AidotCameraNumber(c, desc)
            for c in new_coords.values()
            for desc in CAMERA_NUMBERS
        ]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_numbers()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_numbers()))


class AidotCameraNumber(AidotEntity, NumberEntity):
    """A number entity for an Aidot camera setting."""

    entity_description: AidotNumberDescription

    def __init__(
        self,
        coordinator: AidotCameraUpdateCoordinator,
        description: AidotNumberDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_value(self.coordinator.data)

    async def async_set_native_value(self, value: float) -> None:
        await self.async_run_command(
            self.entity_description.async_set_fn(self.device_client, value),
            f"set {self.name}",
        )
