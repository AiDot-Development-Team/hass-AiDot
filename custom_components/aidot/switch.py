"""Support for AiDot switches."""

import asyncio
import logging
from typing import Any

from aidot.client import AidotClient
from aidot.device_client import DeviceClient
from aidot.exceptions import AidotNotLogin

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Switch."""
    data = hass.data[DOMAIN][entry.entry_id]
    client: AidotClient = data["client"]
    devices: list[dict[str, Any]] = data["devices"]

    async_add_entities(
        AidotSwitch(client, device_info)
        for device_info in devices
        if device_info.get("type") == "switch"
        and "aesKey" in device_info
        and device_info["aesKey"][0] is not None
    )


class AidotSwitch(SwitchEntity):
    """Representation of a Aidot Wi-Fi Switch."""

    _attr_has_entity_name = True

    def __init__(self, client: AidotClient, device: dict[str, Any]) -> None:
        """Initialize the switch."""
        super().__init__()
        self.device_client: DeviceClient = client.get_device_client(device)
        self._attr_unique_id = self.device_client.info.dev_id

        manufacturer = self.device_client.info.model_id.split(".")[0]
        model = self.device_client.info.model_id[len(manufacturer) + 1 :]
        mac = format_mac(self.device_client.info.mac)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            connections={(CONNECTION_NETWORK_MAC, mac)},
            manufacturer=manufacturer,
            model=model,
            name=self.device_client.info.name,
            hw_version=self.device_client.info.hw_version,
        )

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        await super().async_added_to_hass()
        await self.device_client.async_login()
        self.update_task = self.hass.loop.create_task(self._async_update_loop())

    async def async_will_remove_from_hass(self) -> None:
        """Run when entity will be removed from hass."""
        if hasattr(self, "update_task"):
            self.update_task.cancel()
        await super().async_will_remove_from_hass()

    async def _async_update_loop(self):
        """Loop to update status."""
        while True:
            try:
                await self.device_client.read_status()
                self.async_write_ha_state()
            except AidotNotLogin:
                await self.device_client.async_login()
            except Exception as e:
                _LOGGER.error(f"Error in update loop: {e}")
                await asyncio.sleep(5)

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.device_client.status.online

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        return self.device_client.status.on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self.device_client.async_turn_on()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self.device_client.async_turn_off()
