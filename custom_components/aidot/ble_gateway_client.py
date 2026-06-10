"""BLE mesh device client that proxies commands through an AiDot hub."""

from __future__ import annotations

import asyncio
import ctypes
import json
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from aidot.aes_utils import aes_encrypt as _aes_encrypt_raw, aes_decrypt as _aes_decrypt_raw

_HUB_PORT = 10000
_TIMEOUT = 8
_MAX_RETRIES = 3
_BACKOFF = 0.5


def _make_aes_key(key_str: str) -> bytes:
    k = bytearray(16)
    b = key_str.encode()
    k[: len(b)] = b
    return bytes(k)


def _aes_encrypt(data: bytes, key: bytes) -> bytes:
    return _aes_encrypt_raw(data, key)


def _aes_decrypt(data: bytes, key: bytes) -> str:
    return _aes_decrypt_raw(data, key)  # library returns str


def _frame(msg: bytes, key: bytes) -> bytes:
    enc = _aes_encrypt(msg, key)
    return struct.pack(">Hhi", 0x1EED, 1, len(enc)) + enc


async def _recv(reader: asyncio.StreamReader, key: bytes) -> dict:
    hdr = await asyncio.wait_for(reader.readexactly(8), timeout=_TIMEOUT)
    _, _, size = struct.unpack(">HHI", hdr)
    body = await asyncio.wait_for(reader.readexactly(size), timeout=_TIMEOUT)
    raw = _aes_decrypt_raw(body, key)
    return json.loads(raw)


@dataclass
class BleDeviceInfo:
    dev_id: str
    model_id: str
    mac: str
    name: str
    hw_version: str | None
    enable_rgbw: bool
    enable_cct: bool
    cct_min: int = 2700
    cct_max: int = 6500


@dataclass
class BleDeviceStatus:
    online: bool = True
    on: bool = False
    dimming: int = 255
    cct: int = 2700
    rgbw: tuple[int, int, int, int] = field(default_factory=lambda: (255, 255, 255, 0))


_seq = 0


def _next_seq() -> str:
    global _seq
    _seq += 1
    return "ha93" + str(_seq).zfill(5)


class _HubSession:
    """One persistent, serialised connection to an AiDot hub.

    All BLE devices behind the same hub share a single session, so a per-hub
    lock serialises commands (the hub resets the socket on concurrent logins)
    and one logged-in connection is reused across commands (removing the
    per-command connect+login latency). Transient drops are retried; the retry
    path also recovers a connection the hub closed while idle.
    """

    def __init__(
        self,
        hub_id: str,
        hub_ip: str,
        aes_key: bytes,
        user_id: str,
        hub_password: str,
    ) -> None:
        self._hub_id = hub_id
        self._hub_ip = hub_ip
        self._aes_key = aes_key
        self._user_id = user_id
        self._hub_password = hub_password
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._asc = 1

    async def send_command(self, dev_id: str, attr: dict) -> None:
        """Serialise, (re)connect as needed, and send one setDevAttr command."""
        async with self._lock:
            for attempt in range(_MAX_RETRIES):
                try:
                    await self._ensure_connected()
                    await self._send_set_attr(dev_id, attr)
                    return
                except (
                    ConnectionError,
                    asyncio.TimeoutError,
                    asyncio.IncompleteReadError,
                ):
                    await self._close()
                    if attempt == _MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(_BACKOFF * (attempt + 1))

    async def _ensure_connected(self) -> None:
        if self._writer is None or self._writer.is_closing():
            await self._connect_and_login()

    async def _connect_and_login(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._hub_ip, _HUB_PORT), timeout=_TIMEOUT
        )
        seq = str(int(time.time() * 1000) + 1)[-9:]
        login = json.dumps({
            "service": "device",
            "method": "loginReq",
            "seq": seq,
            "srcAddr": self._user_id,
            "deviceId": self._hub_id,
            "payload": {
                "userId": self._user_id,
                "password": self._hub_password,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
                "ascNumber": 1,
            },
        }).encode()
        self._writer.write(_frame(login, self._aes_key))
        await self._writer.drain()
        resp = await _recv(self._reader, self._aes_key)
        self._asc = resp.get("payload", {}).get("ascNumber", 1) + 1

    async def _send_set_attr(self, dev_id: str, attr: dict) -> None:
        cmd = json.dumps({
            "method": "setDevAttrReq",
            "service": "device",
            "clientId": "ha-" + self._user_id,
            "srcAddr": "0." + self._user_id,
            "seq": _next_seq(),
            "payload": {
                "devId": dev_id,
                "parentId": self._hub_id,
                "userId": self._user_id,
                "password": self._hub_password,
                "attr": attr,
                "channel": "ble",
                "ascNumber": self._asc,
            },
            "tst": int(time.time() * 1000),
            "deviceId": dev_id,
        }).encode()
        self._asc += 1
        self._writer.write(_frame(cmd, self._aes_key))
        await self._writer.drain()
        await _recv(self._reader, self._aes_key)

    async def _close(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        if writer is not None:
            try:
                writer.close()
            except Exception:  # noqa: BLE001 - closing is best-effort
                pass


_HUB_SESSIONS: dict[str, _HubSession] = {}


def _get_hub_session(
    hub_id: str, hub_ip: str, aes_key: bytes, user_id: str, hub_password: str
) -> _HubSession:
    """Return the shared session for a hub, creating it on first use."""
    session = _HUB_SESSIONS.get(hub_id)
    if session is None or session._hub_ip != hub_ip:
        session = _HubSession(hub_id, hub_ip, aes_key, user_id, hub_password)
        _HUB_SESSIONS[hub_id] = session
    return session


async def close_all_hub_sessions() -> None:
    """Close every open hub connection and clear the registry (on unload)."""
    for session in list(_HUB_SESSIONS.values()):
        await session._close()
    _HUB_SESSIONS.clear()


class BleGatewayDeviceClient:
    """Controls a BLE mesh light by proxying commands through the AiDot hub's TCP port."""

    def __init__(self, device: dict, hub_device: dict, user_id: str) -> None:
        self._device_id: str = device["id"]
        self._hub_id: str = hub_device["id"]
        self._hub_ip: str = hub_device.get("properties", {}).get("ipAddress", "")
        self._hub_password: str = hub_device.get("password", "")
        self._user_id: str = user_id
        self._aes_key: bytes = _make_aes_key(
            (hub_device.get("aesKey") or [None])[0] or ""
        )

        modules = {
            m["identity"]
            for m in device.get("product", {}).get("serviceModules", [])
        }
        enable_rgbw = "control.light.rgbw" in modules
        enable_cct = "control.light.cct" in modules

        cct_min, cct_max = 2700, 6500
        for m in device.get("product", {}).get("serviceModules", []):
            if m["identity"] == "control.light.cct":
                for p in m.get("properties", []):
                    if p.get("identity") == "CCT":
                        try:
                            cct_min = int(p.get("minValue", cct_min))
                            cct_max = int(p.get("maxValue", cct_max))
                        except (ValueError, TypeError):
                            pass

        self.info = BleDeviceInfo(
            dev_id=self._device_id,
            model_id=device.get("modelId", "unknown"),
            mac=device.get("mac", ""),
            name=device.get("name", ""),
            hw_version=device.get("hardwareVersion"),
            enable_rgbw=enable_rgbw,
            enable_cct=enable_cct,
            cct_min=cct_min,
            cct_max=cct_max,
        )

        props = device.get("properties", {})
        raw_dim = int(props.get("Dimming", 100))
        raw_rgbw = int(props.get("RGBW", 0))
        rgbw_u = ctypes.c_uint32(raw_rgbw).value

        self.status = BleDeviceStatus(
            online=True,
            on=int(props.get("OnOff", 0)) == 1,
            dimming=int(raw_dim * 255 / 100),
            cct=int(props.get("CCT", cct_min)),
            rgbw=(
                (rgbw_u >> 24) & 0xFF,
                (rgbw_u >> 16) & 0xFF,
                (rgbw_u >> 8) & 0xFF,
                rgbw_u & 0xFF,
            ),
        )
        self.on_status_update: Any = None

    async def _send(self, attr: dict) -> None:
        session = _get_hub_session(
            self._hub_id,
            self._hub_ip,
            self._aes_key,
            self._user_id,
            self._hub_password,
        )
        await session.send_command(self._device_id, attr)

        if "OnOff" in attr:
            self.status.on = bool(attr["OnOff"])
        if "Dimming" in attr:
            self.status.dimming = int(attr["Dimming"] * 255 / 100)
        if "CCT" in attr:
            self.status.cct = attr["CCT"]
        if "RGBW" in attr:
            packed = ctypes.c_uint32(attr["RGBW"]).value
            self.status.rgbw = (
                (packed >> 24) & 0xFF,
                (packed >> 16) & 0xFF,
                (packed >> 8) & 0xFF,
                packed & 0xFF,
            )
        if self.on_status_update:
            self.on_status_update(self.status)

    async def async_turn_on(self) -> None:
        await self._send({"OnOff": 1})

    async def async_turn_off(self) -> None:
        await self._send({"OnOff": 0})

    async def async_set_brightness(self, brightness: int) -> None:
        await self._send({"OnOff": 1, "Dimming": int(brightness * 100 / 255)})

    async def async_set_rgbw(self, rgbw: tuple[int, int, int, int]) -> None:
        packed = (rgbw[0] << 24) | (rgbw[1] << 16) | (rgbw[2] << 8) | rgbw[3]
        await self._send({"OnOff": 1, "RGBW": ctypes.c_int32(packed).value})

    async def async_set_cct(self, cct: int) -> None:
        await self._send({"OnOff": 1, "CCT": cct})
