"""Support for Aidot camera select entities."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator
from .entity import AidotEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AidotSelectDescription(SelectEntityDescription):
    """Describes an Aidot camera select entity."""

    get_current_option: Any = None       # callable(DeviceStatusData) -> str | None
    async_select_option_fn: Any = None   # async callable(DeviceClient, str) -> None
    # Optimistic = write-only control with no cloud readback (e.g. resolution,
    # which rides the active stream session): store the chosen value and apply
    # best-effort, instead of erroring when the camera isn't streaming.
    optimistic: bool = False


CAMERA_SELECTS: tuple[AidotSelectDescription, ...] = (
    AidotSelectDescription(
        key="night_vision",
        translation_key="night_vision",
        icon="mdi:weather-night",
        entity_category=EntityCategory.CONFIG,
        options=["auto", "on", "off"],
        get_current_option=lambda s: s.night_vision_mode,
        async_select_option_fn=lambda c, v: c.async_set_night_vision(v),
    ),
    # Resolution rides the active stream session (SETSTREAMCTRL=800), so the
    # camera must be streaming for a change to take effect, and there's no cloud
    # property to read it back - get_current_option is None, so the select holds
    # the last-chosen value optimistically (mirrors the app's HD/SD toggle).
    AidotSelectDescription(
        key="resolution",
        translation_key="resolution",
        icon="mdi:high-definition",
        entity_category=EntityCategory.CONFIG,
        options=["hd", "sd"],
        get_current_option=None,
        async_select_option_fn=lambda c, v: c.async_set_resolution(v),
        optimistic=True,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Aidot camera select entities."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_selects() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered
        }
        new = [
            (AidotResolutionSelect if desc.optimistic else AidotCameraSelect)(c, desc)
            for c in new_coords.values()
            for desc in CAMERA_SELECTS
        ]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_selects()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_selects()))


class AidotCameraSelect(AidotEntity, SelectEntity):
    """A select backed by a cloud-polled device attribute."""

    entity_description: AidotSelectDescription

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotSelectDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description
        self._attr_options = list(description.options)

    @property
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.entity_description.get_current_option(self.coordinator.data)

    async def async_select_option(self, option: str) -> None:
        await self.async_run_command(
            self.entity_description.async_select_option_fn(self.device_client, option),
            f"set {self.name} to {option}",
        )


class AidotResolutionSelect(RestoreEntity, AidotCameraSelect):
    """A write-only select with no cloud readback (e.g. resolution).

    Holds the chosen value optimistically (restored across restarts, seeded to a
    default on a fresh install so it never shows "unknown") and applies it
    best-effort over the active stream session - the library setter returns
    False, not raises, when the camera isn't streaming, so the change simply
    takes effect on the next/active session.
    """

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotSelectDescription,
    ) -> None:
        super().__init__(coordinator, description)
        self._optimistic_option: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._optimistic_option is None:
            last = await self.async_get_last_state()
            if last is not None and last.state in self._attr_options:
                self._optimistic_option = last.state
            else:
                self._optimistic_option = self._attr_options[0]  # default (e.g. "hd")

    @property
    def current_option(self) -> str | None:
        return self._optimistic_option

    async def async_select_option(self, option: str) -> None:
        self._optimistic_option = option
        self.async_write_ha_state()
        try:
            await self.entity_description.async_select_option_fn(
                self.device_client, option
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("AiDot %s best-effort apply failed: %s", self.name, exc)
