"""PTZ button entities for Aidot cameras."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import AidotCameraUpdateCoordinator, AidotConfigEntry
from .entity import AidotEntity


@dataclass(frozen=True, kw_only=True)
class AidotButtonDescription(ButtonEntityDescription):
    """Describes an Aidot PTZ button."""

    async_press_fn: object = None  # async callable(DeviceClient) -> bool


# Map TUTK IOCtrl direction codes (from ptzDirection product property) to button keys.
# 1=up, 2=down, 3=left, 6=right — confirmed from live device data (pan-only = [3,6]).
_DIRECTION_CODE_KEYS: dict[int, str] = {
    1: "ptz_up",
    2: "ptz_down",
    3: "ptz_left",
    6: "ptz_right",
}

PTZ_BUTTONS: tuple[AidotButtonDescription, ...] = (
    AidotButtonDescription(
        key="ptz_up",
        translation_key="ptz_up",
        icon="mdi:arrow-up-circle-outline",
        entity_category=EntityCategory.CONFIG,
        async_press_fn=lambda c: c.async_ptz_move("up"),
    ),
    AidotButtonDescription(
        key="ptz_down",
        translation_key="ptz_down",
        icon="mdi:arrow-down-circle-outline",
        entity_category=EntityCategory.CONFIG,
        async_press_fn=lambda c: c.async_ptz_move("down"),
    ),
    AidotButtonDescription(
        key="ptz_left",
        translation_key="ptz_left",
        icon="mdi:arrow-left-circle-outline",
        entity_category=EntityCategory.CONFIG,
        async_press_fn=lambda c: c.async_ptz_move("left"),
    ),
    AidotButtonDescription(
        key="ptz_right",
        translation_key="ptz_right",
        icon="mdi:arrow-right-circle-outline",
        entity_category=EntityCategory.CONFIG,
        async_press_fn=lambda c: c.async_ptz_move("right"),
    ),
    AidotButtonDescription(
        key="ptz_stop",
        translation_key="ptz_stop",
        icon="mdi:stop-circle-outline",
        entity_category=EntityCategory.CONFIG,
        async_press_fn=lambda c: c.async_ptz_stop(),
    ),
    AidotButtonDescription(
        key="ptz_zoom_in",
        translation_key="ptz_zoom_in",
        icon="mdi:magnify-plus-outline",
        entity_category=EntityCategory.CONFIG,
        async_press_fn=lambda c: c.async_ptz_move("zoom_in"),
    ),
    AidotButtonDescription(
        key="ptz_zoom_out",
        translation_key="ptz_zoom_out",
        icon="mdi:magnify-minus-outline",
        entity_category=EntityCategory.CONFIG,
        async_press_fn=lambda c: c.async_ptz_move("zoom_out"),
    ),
)


def _is_ptz_camera(coordinator: AidotCameraUpdateCoordinator) -> bool:
    """Return True if the camera supports PTZ (model A001064)."""
    model_id = coordinator.device_client.info.model_id or ""
    return "A001064" in model_id


def _ptz_buttons_for(coordinator: AidotCameraUpdateCoordinator) -> list[AidotButtonDescription]:
    """Return button descriptions appropriate for this camera's capabilities.

    Uses ptzDirection codes from the product definition to gate which buttons are
    shown.  A pan-only camera advertises [3,6] (left/right) — up/down/zoom are
    suppressed.  When direction codes are unknown (empty list) all buttons are
    returned for backward compatibility.
    """
    dirs = coordinator.device_client.info.ptz_directions  # [] = unknown
    if not dirs:
        return list(PTZ_BUTTONS)

    supported_keys = {_DIRECTION_CODE_KEYS[c] for c in dirs if c in _DIRECTION_CODE_KEYS}
    return [
        desc for desc in PTZ_BUTTONS
        if desc.key == "ptz_stop" or desc.key in supported_keys
        # zoom buttons omitted when capabilities are known but no zoom code present
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Aidot PTZ buttons."""
    coordinator = entry.runtime_data
    registered: set[str] = set()

    def _add_new_buttons() -> None:
        new_coords = {
            dev_id: c
            for dev_id, c in coordinator.camera_coordinators.items()
            if dev_id not in registered and _is_ptz_camera(c)
        }
        new = [
            AidotPtzButton(c, desc)
            for c in new_coords.values()
            for desc in _ptz_buttons_for(c)
        ]
        if new:
            registered.update(new_coords)
            async_add_entities(new)

    _add_new_buttons()
    entry.async_on_unload(coordinator.async_add_listener(lambda: _add_new_buttons()))


class AidotPtzButton(AidotEntity, ButtonEntity):
    """A button that sends one PTZ command when pressed."""

    entity_description: AidotButtonDescription

    def __init__(
        self,
        coordinator: AidotCameraUpdateCoordinator,
        description: AidotButtonDescription,
    ) -> None:
        super().__init__(coordinator, key=description.key)
        self.entity_description = description

    async def async_press(self) -> None:
        await self.async_run_command(
            self.entity_description.async_press_fn(self.device_client),
            f"{self.name}",
        )
