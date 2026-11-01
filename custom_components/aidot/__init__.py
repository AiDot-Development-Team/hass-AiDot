"""The aidot integration."""

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import AidotConfigEntry, AidotDeviceManagerCoordinator
from .logging_utils import configure_library_logging

configure_library_logging()

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.LIGHT,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: AidotConfigEntry) -> bool:
    """Set up aidot from a config entry."""

    coordinator = AidotDeviceManagerCoordinator(hass, entry)
    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_cleanup()
        raise
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AidotConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_cleanup()
    return unloaded
