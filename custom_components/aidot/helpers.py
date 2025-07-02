"""Helper classes for the AiDot integration."""
import asyncio
import logging
from typing import Any

from aidot.client import AidotClient
from aidot.const import CONF_ID
from aidot.device_client import DeviceClient

_LOGGER = logging.getLogger(__name__)


class PatchedDeviceClient(DeviceClient):
    """A wrapper for the DeviceClient that includes patches and workarounds."""

    def __init__(self, *args, **kwargs):
        """Initialize the patched client."""
        super().__init__(*args, **kwargs)
        # Fix for 'AttributeError: 'DeviceClient' object has no attribute 'writer''
        self.writer = None
        self.reader = None
        self._manual_ip = False

    def update_ip_address(self, ip: str, manual: bool = False) -> None:
        """Update the device's IP address, with a lock for manual IPs."""
        if self._manual_ip and not manual:
            _LOGGER.debug(
                f"Ignoring discovered IP {ip} for {self.device_id} because a manual IP is set."
            )
            return
        self._ip_address = ip
        if manual:
            self._manual_ip = True


class PatchedAidotClient(AidotClient):
    """A wrapper for the AidotClient that uses our patched device client."""

    def get_device_client(self, device: dict[str, Any]) -> PatchedDeviceClient:
        """Get or create a patched device client."""
        device_id = device.get(CONF_ID)
        device_client: PatchedDeviceClient = self._device_clients.get(device_id)
        if device_client is None:
            device_client = PatchedDeviceClient(device, self.login_info)
            self._device_clients[device_id] = device_client
            asyncio.get_running_loop().create_task(device_client.ping_task())

        # This part is for discovery, which we want to keep.
        if self._discover is not None:
            ip = self._discover.discovered_device.get(device_id)
            if ip is not None:
                # update_ip_address on PatchedDeviceClient is called here.
                # manual is False by default, which is correct for discovery.
                device_client.update_ip_address(ip)

        return device_client
