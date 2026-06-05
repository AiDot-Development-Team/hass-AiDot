"""Support for Aidot camera binary sensors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator
from .entity import AidotEntity

# How long a live motion event implies "occupied" before we fall back to the
# cloud-polled value. The cloud Occupancy attribute only refreshes on the 5-min
# camera poll, so motion events (polled every ~30 s) give a much faster signal.
MOTION_OCCUPANCY_WINDOW = 120.0


@dataclass(frozen=True, kw_only=True)
class AidotBinarySensorDescription(BinarySensorEntityDescription):
    """Describes an Aidot camera binary sensor."""

    get_is_on: Any = None  # callable(DeviceStatusData) -> bool | None
    # Also drive the sensor ON from live cloud motion events (OR'd with the
    # slower cloud-polled value) for a near-real-time presence signal.
    motion_live: bool = False


CAMERA_BINARY_SENSORS: tuple[AidotBinarySensorDescription, ...] = (
    AidotBinarySensorDescription(
        key="occupancy",
        translation_key="occupancy",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        get_is_on=lambda s: s.occupancy,
        motion_live=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Aidot camera binary sensors."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_binary_sensors() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new = [
            (
                AidotOccupancyBinarySensor
                if desc.motion_live
                else AidotCameraBinarySensor
            )(c, desc)
            for c in new_coords.values()
            for desc in CAMERA_BINARY_SENSORS
        ]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_binary_sensors()
    entry.async_on_unload(
        coordinator.async_add_listener(lambda: _add_new_binary_sensors())
    )


class AidotCameraBinarySensor(AidotEntity, BinarySensorEntity):
    """A read-only binary sensor backed by a cloud-polled camera attribute."""

    entity_description: AidotBinarySensorDescription

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_is_on(self.coordinator.data)


class AidotOccupancyBinarySensor(AidotCameraBinarySensor):
    """Occupancy sensor that OR's the cloud-polled value with live motion events.

    The cloud Occupancy attribute only refreshes on the 5-min camera poll, so a
    motion event (polled ~every 30 s) flips presence on much sooner; the sensor
    falls back to the cloud value once the motion window lapses.
    """

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, description)
        self._last_motion: float | None = None
        self._motion_expiry_unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.add_motion_listener(self._on_motion)
        )
        # Cancel any pending expiry timer on removal so it can't fire
        # async_write_ha_state() on a dead entity (and keep it alive).
        self.async_on_remove(self._cancel_motion_timer)

    @callback
    def _cancel_motion_timer(self) -> None:
        if self._motion_expiry_unsub is not None:
            self._motion_expiry_unsub()
            self._motion_expiry_unsub = None

    @callback
    def _on_motion(self, _event: dict) -> None:
        """A live cloud motion event arrived: mark occupied for the window."""
        self._last_motion = self.hass.loop.time()
        if self._motion_expiry_unsub is not None:
            self._motion_expiry_unsub()
        # Re-evaluate when the window lapses so the sensor can fall back off.
        self._motion_expiry_unsub = async_call_later(
            self.hass, MOTION_OCCUPANCY_WINDOW + 1.0, self._on_motion_expired
        )
        self.async_write_ha_state()

    @callback
    def _on_motion_expired(self, _now) -> None:
        self._motion_expiry_unsub = None
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool | None:
        # A live motion event within the window wins (fast presence signal);
        # otherwise fall back to the slower cloud-polled value (super()).
        if (
            self._last_motion is not None
            and self.hass.loop.time() - self._last_motion < MOTION_OCCUPANCY_WINDOW
        ):
            return True
        return super().is_on
