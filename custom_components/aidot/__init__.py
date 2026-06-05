"""The aidot integration."""

import os

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONF_SERVE_PORT_BASE
from .coordinator import AidotConfigEntry, AidotDeviceManagerCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: AidotConfigEntry) -> bool:
    """Set up aidot from a config entry."""
    # Apply the optional SDES HTTP-serve port base (camera._serve_port reads this).
    if (port_base := entry.options.get(CONF_SERVE_PORT_BASE)) is not None:
        os.environ["AIDOT_SERVE_PORT_BASE"] = str(port_base)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))

    coordinator = AidotDeviceManagerCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload_on_options(hass: HomeAssistant, entry: AidotConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AidotConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_cleanup()
    return ok
