"""Diagnostics support for the AiDot integration."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .ble_gateway_client import BleGatewayDeviceClient
from .coordinator import AidotConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: AidotConfigEntry
) -> dict[str, Any]:
    """Return redacted passive BLE listener diagnostics."""
    del hass
    devices: list[dict[str, Any]] = []
    for coordinator in entry.runtime_data.device_coordinators.values():
        client = coordinator.device_client
        if isinstance(client, BleGatewayDeviceClient):
            devices.append(await client.async_get_diagnostics())

    return {
        "cloudSession": entry.runtime_data.cloud_status.copy(),
        "cloudGroups": {
            **entry.runtime_data.group_status,
            "connection": entry.runtime_data.group_cloud.diagnostics.copy(),
            "groups": [
                {
                    **coordinator.device_client.report,
                    "available": coordinator.device_client.status.online,
                    "assumedState": True,
                    "lastAcknowledgementMs": coordinator.device_client.last_acknowledgement_ms,
                }
                for coordinator in entry.runtime_data.group_coordinators.values()
            ],
        },
        "bleGatewayDeviceCount": len(devices),
        "bleGatewayDevices": devices,
    }
