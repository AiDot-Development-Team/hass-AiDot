"""BLE mesh device client that proxies commands through an AiDot hub."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from aidot.aes_utils import (
    aes_decrypt as _aes_decrypt_raw,
    aes_encrypt as _aes_encrypt_raw,
)

_HUB_PORT = 10000
_TIMEOUT = 8
_DIAGNOSTIC_FOLLOWUP_TIMEOUT = 1
_MAX_RETRIES = 3
_BACKOFF = 0.5
_MAX_RECONNECT_BACKOFF = 60
_HUB_KEEPALIVE_INTERVAL = 30
_HUB_IDLE_TIMEOUT = 90
_MAX_IN_FLIGHT = 4

_LOGGER = logging.getLogger(__name__)

_DIAGNOSTIC_REDACT_KEYS = {
    "accesstoken",
    "aeskey",
    "area",
    "blemeshdevicekey",
    "city",
    "citytimezone",
    "clientid",
    "deviceid",
    "devid",
    "directgateway",
    "email",
    "houseid",
    "id",
    "ipaddress",
    "lat",
    "latitude",
    "lon",
    "longitude",
    "mac",
    "parentid",
    "password",
    "refreshtoken",
    "roomid",
    "spaceid",
    "srcaddr",
    "ssidname",
    "token",
    "userid",
    "username",
}
_DIAGNOSTIC_REDACT_TERMS = (
    "credential",
    "password",
    "secret",
    "token",
)
_REDACTED = "**REDACTED**"


def _make_aes_key(key_str: str) -> bytes:
    k = bytearray(16)
    b = key_str.encode()
    k[: len(b)] = b
    return bytes(k)


def _aes_encrypt(data: bytes, key: bytes) -> bytes:
    return _aes_encrypt_raw(data, key)


def _aes_decrypt(data: bytes, key: bytes) -> str:
    return _aes_decrypt_raw(data, key)  # library returns str


def _frame(msg: bytes, key: bytes, message_type: int = 1) -> bytes:
    enc = _aes_encrypt(msg, key)
    return struct.pack(">Hhi", 0x1EED, message_type, len(enc)) + enc


async def _recv(
    reader: asyncio.StreamReader,
    key: bytes,
    timeout: float | None = _TIMEOUT,
) -> dict:
    if timeout is None:
        hdr = await reader.readexactly(8)
    else:
        hdr = await asyncio.wait_for(reader.readexactly(8), timeout=timeout)
    _, _, size = struct.unpack(">HHI", hdr)
    if timeout is None:
        body = await reader.readexactly(size)
    else:
        body = await asyncio.wait_for(reader.readexactly(size), timeout=timeout)
    raw = _aes_decrypt_raw(body, key)
    return json.loads(raw)


def _is_diagnostic_sensitive_key(key: object) -> bool:
    """Return whether a diagnostic mapping key may identify or authenticate."""
    key_lower = str(key).lower()
    return (
        key_lower in _DIAGNOSTIC_REDACT_KEYS
        or key_lower.endswith("key")
        or any(term in key_lower for term in _DIAGNOSTIC_REDACT_TERMS)
    )


def _diagnostic_secret_values(value: Any, sensitive: bool = False) -> set[str]:
    """Collect string values stored below sensitive mapping keys."""
    secrets: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            secrets.update(
                _diagnostic_secret_values(
                    item, sensitive=sensitive or _is_diagnostic_sensitive_key(key)
                )
            )
    elif isinstance(value, (list, tuple)):
        for item in value:
            secrets.update(_diagnostic_secret_values(item, sensitive=sensitive))
    elif sensitive and isinstance(value, str) and value:
        secrets.add(value)
    return secrets


def _redact_diagnostics(
    value: Any, sensitive_values: set[str] | None = None
) -> Any:
    """Recursively redact credentials and identifiers from diagnostic data."""
    sensitive_values = sensitive_values or set()
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_diagnostic_sensitive_key(key_text):
                redacted[key_text] = _REDACTED
            else:
                redacted[key_text] = _redact_diagnostics(item, sensitive_values)
        return redacted
    if isinstance(value, list):
        return [_redact_diagnostics(item, sensitive_values) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_diagnostics(item, sensitive_values) for item in value)
    if isinstance(value, str) and value in sensitive_values:
        return _REDACTED
    return value


def _diagnostic_attributes(device: dict[str, Any]) -> list[str]:
    """Return readable attribute names advertised for a BLE mesh device."""
    attributes = {
        str(key)
        for key in device.get("properties", {})
        if not _is_diagnostic_sensitive_key(key)
    }
    for module in device.get("product", {}).get("serviceModules", []):
        for prop in module.get("properties", []):
            identity = prop.get("identity")
            if isinstance(identity, str) and identity:
                attributes.add(identity)
    return sorted(attributes)


def _advertises_charge_state(device: dict[str, Any]) -> bool:
    """Distinguish a product capability from a generic cloud property."""
    return any(
        prop.get("identity") == "chargeState"
        for module in device.get("product", {}).get("serviceModules", [])
        for prop in module.get("properties", [])
    )


def _parse_int(value: Any, minimum: int, maximum: int) -> int | None:
    """Return an integer within a known telemetry range."""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if minimum <= parsed <= maximum:
        return parsed
    return None


def _parse_bool(value: Any) -> bool | None:
    """Return a boolean for the common AiDot 0/1 representation."""
    parsed = _parse_int(value, 0, 1)
    return bool(parsed) if parsed is not None else None


def _parse_alarm(value: Any) -> bool | None:
    """Treat any non-zero alarm code as active."""
    parsed = _parse_int(value, 0, 9999999999)
    return bool(parsed) if parsed is not None else None


def _parse_percentage_factor(value: Any) -> int | None:
    """Convert either a 0-1 factor or 0-100 value to a percentage."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if 0 <= parsed <= 1:
        parsed *= 100
    if 0 <= parsed <= 100:
        return round(parsed)
    return None


def _effect_codes(device: dict[str, Any]) -> dict[str, int]:
    """Return effect names and codes advertised by the product metadata."""
    effects: dict[str, int] = {}
    for module in device.get("product", {}).get("serviceModules", []):
        if module.get("identity") != "control.light.effect.mode":
            continue
        for prop in module.get("properties", []):
            for option in prop.get("allowedValues", []):
                code = _parse_int(option.get("value"), 0, 65535)
                if code is None:
                    continue
                name = option.get("description") or option.get("name")
                if not isinstance(name, str) or not name.strip():
                    name = f"Effect {code}"
                effects[name.strip().title()] = code
    return effects


@dataclass
class BleDeviceInfo:
    dev_id: str
    model_id: str
    mac: str
    name: str
    hw_version: str | None
    enable_rgbw: bool
    enable_cct: bool
    effects: dict[str, int] = field(default_factory=dict)
    cct_min: int = 2700
    cct_max: int = 6500


@dataclass
class BleDeviceStatus:
    online: bool = False
    on: bool = False
    dimming: int = 255
    cct: int = 2700
    rgbw: tuple[int, int, int, int] = field(default_factory=lambda: (255, 255, 255, 0))
    battery: int | None = None
    mesh_rssi: int | None = None
    charging: bool | None = None
    telemetry_connected: bool | None = None
    last_telemetry_update: datetime | None = None
    energy_saving_enabled: bool | None = None
    energy_saving_factor: int | None = None
    light_duration: int | None = None
    detection_mode: int | None = None
    security_alarm: bool | None = None
    alarm_status: bool | None = None
    effect_mode: int | None = None
    active_color_mode: str | None = None


class HubCommandError(Exception):
    """A command reached the hub but was explicitly rejected."""


_seq = 0


def _next_seq() -> str:
    global _seq
    _seq += 1
    return "ha93" + str(_seq).zfill(5)


class _HubSession:
    """Maintain one passive, multiplexed connection to an AiDot BLE hub."""

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
        self._connection_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._command_slots = asyncio.Semaphore(_MAX_IN_FLIGHT)
        self._group_request_lock = asyncio.Lock()
        self._device_request_locks: dict[str, asyncio.Lock] = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Queue[dict | BaseException]] = {}
        self._early_responses: dict[str, list[dict[str, Any]]] = {}
        self._device_listeners: dict[
            str, Callable[[dict[str, Any]], None]
        ] = {}
        self._connection_listeners: dict[str, Callable[[bool], None]] = {}
        self._closing = False
        self._asc = 1
        self._received_frames = 0
        self._unrouted_updates = 0
        self._last_hub_message: datetime | None = None
        self._device_update_counts: dict[str, int] = {}
        self._last_device_update: dict[str, datetime] = {}
        self._last_device_attributes: dict[str, tuple[str, ...]] = {}
        self._on_login_failure: Callable[[], None] | None = None
        self._connection_stage = "disconnected"
        self._last_connection_error: dict[str, Any] | None = None
        self._connection_attempts = 0
        self._last_successful_login: datetime | None = None
        self._acknowledged_commands = 0
        self._max_pending_requests = 0
        self._last_command_latency_ms: float | None = None

    async def async_update_connection(
        self,
        hub_ip: str,
        aes_key: bytes,
        user_id: str,
        hub_password: str,
        on_login_failure: Callable[[], None] | None = None,
    ) -> None:
        """Refresh the shared session in place, preserving every light listener."""
        self._on_login_failure = on_login_failure
        if (hub_ip, aes_key, user_id, hub_password) == (
            self._hub_ip, self._aes_key, self._user_id, self._hub_password
        ):
            return
        async with self._request_lock:
            await self._stop_reconnect()
            async with self._connection_lock:
                await self._drop_connection()
                self._hub_ip = hub_ip
                self._aes_key = aes_key
                self._user_id = user_id
                self._hub_password = hub_password
                self._closing = False

    @property
    def connected(self) -> bool:
        """Return whether the passive hub listener is connected."""
        return (
            self._writer is not None
            and not self._writer.is_closing()
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    def register_device(
        self,
        dev_id: str,
        listener: Callable[[dict[str, Any]], None],
        connection_listener: Callable[[bool], None],
    ) -> None:
        """Register a BLE device to receive attributes routed by device ID."""
        self._device_listeners[dev_id] = listener
        self._connection_listeners[dev_id] = connection_listener
        connection_listener(self.connected)

    def unregister_device(self, dev_id: str) -> None:
        """Stop routing passive hub updates to a removed BLE device."""
        self._device_listeners.pop(dev_id, None)
        self._connection_listeners.pop(dev_id, None)

    async def stop_if_unused(self) -> None:
        """Close a hub session after its final BLE device is removed."""
        if not self._device_listeners:
            await self.close()
            if _HUB_SESSIONS.get(self._hub_id) is self:
                _HUB_SESSIONS.pop(self._hub_id)

    async def start(self) -> None:
        """Start the passive listener without failing integration setup."""
        self._closing = False
        if self.connected or (
            self._reconnect_task is not None and not self._reconnect_task.done()
        ):
            return
        try:
            await self._ensure_connected()
        except Exception as error:  # noqa: BLE001 - reconnect in background
            _LOGGER.debug(
                "BLE hub listener %s unavailable (%s); reconnect scheduled",
                self._anonymous_hub_id,
                type(error).__name__,
            )
            self._schedule_reconnect()

    async def send_command(self, dev_id: str, attr: dict) -> None:
        """Send one command while the session reader owns all socket reads."""
        await self._request("setDevAttrReq", dev_id, attr)

    async def run_group_command(self, operation: Any) -> Any:
        """Drain local requests before dispatching a cloud group operation.

        This orders client dispatch, not physical delivery across transports.
        Retain the normal per-device pipeline outside group operations.
        """
        acquired = 0
        async with self._group_request_lock:
            try:
                for _ in range(_MAX_IN_FLIGHT):
                    await self._command_slots.acquire()
                    acquired += 1
                return await operation()
            finally:
                for _ in range(acquired):
                    self._command_slots.release()

    async def get_attributes(self, dev_id: str, attr: list[str]) -> list[dict]:
        """Perform an explicit one-shot read through the passive dispatcher."""
        return await self._request(
            "getDevAttrReq", dev_id, attr, collect_attribute_response=True
        )

    async def _request(
        self,
        method: str,
        dev_id: str,
        attr: dict | list[str],
        *,
        collect_attribute_response: bool = False,
    ) -> list[dict]:
        """Pipeline different lights, retaining command order for each light."""
        device_lock = self._device_request_locks.setdefault(dev_id, asyncio.Lock())
        async with device_lock, self._command_slots:
            return await self._request_with_retries(
                method, dev_id, attr, collect_attribute_response=collect_attribute_response
            )

    async def _request_with_retries(
        self,
        method: str,
        dev_id: str,
        attr: dict | list[str],
        *,
        collect_attribute_response: bool,
    ) -> list[dict]:
        """Serialize socket writes; wait for correlated replies outside the lock."""
        for attempt in range(_MAX_RETRIES):
            seq: str | None = None
            request_reader: asyncio.StreamReader | None = None
            try:
                async with self._request_lock:
                    await self._ensure_connected()
                    request_reader = self._reader
                    seq, command = self._make_attr_request(method, dev_id, attr)
                    response_queue: asyncio.Queue[dict | BaseException] = (
                        asyncio.Queue()
                    )
                    self._pending[seq] = response_queue
                    self._max_pending_requests = max(
                        self._max_pending_requests, len(self._pending)
                    )
                    for early_response in self._early_responses.pop(seq, []):
                        response_queue.put_nowait(early_response)
                    if self._writer is None:
                        raise ConnectionError("BLE hub writer is unavailable")
                    command_started = time.monotonic()
                    self._writer.write(_frame(command, self._aes_key))
                    await self._writer.drain()

                responses = [await self._wait_for_response(response_queue, _TIMEOUT)]
                ack = responses[0].get("ack")
                if isinstance(ack, dict) and ack.get("code") not in (None, 200):
                    raise HubCommandError(
                        f"AiDot hub rejected {method} (code {ack['code']})"
                    )
                if method == "setDevAttrReq":
                    self._acknowledged_commands += 1
                    self._last_command_latency_ms = round(
                        (time.monotonic() - command_started) * 1000, 1
                    )
                if collect_attribute_response and not self._has_attributes(responses):
                    try:
                        responses.append(
                            await self._wait_for_response(
                                response_queue, _DIAGNOSTIC_FOLLOWUP_TIMEOUT
                            )
                        )
                    except asyncio.TimeoutError:
                        pass
                return responses
            except (
                ConnectionError,
                asyncio.TimeoutError,
                asyncio.IncompleteReadError,
                OSError,
            ):
                if request_reader is not None:
                    async with self._request_lock, self._connection_lock:
                        # Another pending request may already have reconnected.
                        await self._drop_connection(expected_reader=request_reader)
                if attempt == _MAX_RETRIES - 1 or self._closing:
                    self._schedule_reconnect()
                    raise
                await asyncio.sleep(_BACKOFF * (attempt + 1))
            finally:
                if seq is not None:
                    self._pending.pop(seq, None)
        return []

    async def _ensure_connected(self) -> None:
        if self._closing:
            raise ConnectionError("BLE hub session is closed")
        if self.connected:
            return
        async with self._connection_lock:
            if self.connected:
                return
            try:
                await self._connect_and_login()
            except Exception as error:
                self._record_connection_error(error)
                await self._drop_connection()
                if self._on_login_failure:
                    self._on_login_failure()
                raise

    async def _connect_and_login(self) -> None:
        self._connection_attempts += 1
        self._connection_stage = "tcp_connect"
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
        self._connection_stage = "login_send"
        self._writer.write(_frame(login, self._aes_key))
        await self._writer.drain()
        self._connection_stage = "login_response"
        resp = await _recv(self._reader, self._aes_key)
        ack = resp.get("ack")
        if isinstance(ack, dict) and ack.get("code") not in (None, 200):
            raise ConnectionError(
                f"BLE hub login failed with code {ack.get('code')}"
            )
        self._asc = resp.get("payload", {}).get("ascNumber", 1) + 1
        self._connection_stage = "connected"
        self._last_successful_login = datetime.now(UTC)
        self._reader_task = asyncio.create_task(
            self._receive_loop(self._reader),
            name=f"aidot_ble_hub_receive_{self._anonymous_hub_id}",
        )
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(),
            name=f"aidot_ble_hub_keepalive_{self._anonymous_hub_id}",
        )
        self._notify_connection_state(True)
        _LOGGER.debug("BLE hub listener %s connected", self._anonymous_hub_id)

    def _make_attr_request(
        self, method: str, dev_id: str, attr: dict | list[str]
    ) -> tuple[str, bytes]:
        seq = _next_seq()
        cmd = json.dumps({
            "method": method,
            "service": "device",
            "clientId": "ha-" + self._user_id,
            "srcAddr": "0." + self._user_id,
            "seq": seq,
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
        return seq, cmd

    async def _receive_loop(self, reader: asyncio.StreamReader) -> None:
        """Continuously receive solicited and unsolicited hub frames."""
        cancelled = False
        try:
            while True:
                response = await _recv(
                    reader, self._aes_key, timeout=_HUB_IDLE_TIMEOUT
                )
                self._route_response(response)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except Exception as error:  # noqa: BLE001 - any read failure reconnects
            self._record_connection_error(error)
            _LOGGER.debug(
                "BLE hub listener %s disconnected: %s",
                self._anonymous_hub_id,
                type(error).__name__,
            )
        finally:
            if not cancelled and not self._closing and self._reader is reader:
                self._reader_task = None
                await self._drop_connection(
                    cancel_reader=False, expected_reader=reader
                )
                self._schedule_reconnect()

    def _route_response(self, response: dict[str, Any]) -> None:
        """Route a frame to its waiting request and target BLE device."""
        self._received_frames += 1
        self._last_hub_message = datetime.now(UTC)

        seq = response.get("seq")
        response_queue = self._pending.get(str(seq)) if seq is not None else None
        if response_queue is not None:
            response_queue.put_nowait(response)
        elif isinstance(seq, str) and seq.startswith("ha93"):
            self._early_responses.setdefault(seq, []).append(response)
            while len(self._early_responses) > 20:
                self._early_responses.pop(next(iter(self._early_responses)))

        payload = response.get("payload")
        if not isinstance(payload, dict):
            return
        attributes = payload.get("attr")
        if not isinstance(attributes, dict):
            return

        dev_id = self._response_device_id(response, payload)
        listener = self._device_listeners.get(dev_id) if dev_id else None
        if listener is None:
            self._unrouted_updates += 1
            return

        now = datetime.now(UTC)
        self._device_update_counts[dev_id] = (
            self._device_update_counts.get(dev_id, 0) + 1
        )
        self._last_device_update[dev_id] = now
        self._last_device_attributes[dev_id] = tuple(
            sorted(str(attribute) for attribute in attributes)
        )
        _LOGGER.debug(
            "BLE hub update device=%s method=%s attributes=%s",
            hashlib.sha256(dev_id.encode()).hexdigest()[:12],
            response.get("method", "unknown"),
            self._last_device_attributes[dev_id],
        )
        try:
            listener(attributes)
        except Exception:  # noqa: BLE001 - one entity must not stop the receiver
            _LOGGER.exception(
                "Error applying BLE hub update for device %s",
                hashlib.sha256(dev_id.encode()).hexdigest()[:12],
            )

    @staticmethod
    def _response_device_id(
        response: dict[str, Any], payload: dict[str, Any]
    ) -> str | None:
        """Extract the target device ID from known AiDot frame layouts."""
        payload_dev_id = payload.get("devId")
        if isinstance(payload_dev_id, str) and payload_dev_id:
            return payload_dev_id
        src_addr = response.get("srcAddr")
        if isinstance(src_addr, str) and src_addr.startswith("1."):
            return src_addr.split(".", 1)[1]
        device_id = response.get("deviceId")
        if isinstance(device_id, str) and device_id:
            return device_id
        return None

    @staticmethod
    def _has_attributes(responses: list[dict]) -> bool:
        return any(
            isinstance(response.get("payload"), dict)
            and isinstance(response["payload"].get("attr"), dict)
            for response in responses
        )

    @staticmethod
    async def _wait_for_response(
        response_queue: asyncio.Queue[dict | BaseException], timeout: float
    ) -> dict:
        response = await asyncio.wait_for(response_queue.get(), timeout=timeout)
        if isinstance(response, BaseException):
            raise response
        return response

    async def _keepalive_loop(self) -> None:
        """Keep the powered hub connection alive without querying BLE lights."""
        reader = None
        try:
            while True:
                await asyncio.sleep(_HUB_KEEPALIVE_INTERVAL)
                async with self._request_lock:
                    if not self.connected or self._writer is None:
                        return
                    reader = self._reader
                    ping = json.dumps({
                        "service": "test",
                        "method": "pingreq",
                        "seq": "123456",
                        "srcAddr": "123456",
                        "payload": {},
                    }).encode()
                    self._writer.write(_frame(ping, self._aes_key, message_type=2))
                    await self._writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - reconnect on any socket error
            self._record_connection_error(error)
            _LOGGER.debug(
                "BLE hub keepalive %s failed: %s",
                self._anonymous_hub_id,
                type(error).__name__,
            )
            await self._drop_connection(
                cancel_keepalive=False, expected_reader=reader
            )
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._closing or not self._device_listeners:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(
            self._reconnect_loop(),
            name=f"aidot_ble_hub_reconnect_{self._anonymous_hub_id}",
        )

    async def _reconnect_loop(self) -> None:
        delay = _BACKOFF
        try:
            while not self._closing and self._device_listeners:
                try:
                    await self._ensure_connected()
                    return
                except Exception as error:  # noqa: BLE001 - retry in background
                    _LOGGER.debug(
                        "BLE hub listener %s reconnect failed: %s",
                        self._anonymous_hub_id,
                        type(error).__name__,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, _MAX_RECONNECT_BACKOFF)
        except asyncio.CancelledError:
            raise
        finally:
            if asyncio.current_task() is self._reconnect_task:
                self._reconnect_task = None

    async def _drop_connection(
        self,
        *,
        cancel_reader: bool = True,
        cancel_keepalive: bool = True,
        expected_reader: asyncio.StreamReader | None = None,
    ) -> None:
        """Drop socket tasks and wake any request waiting on the connection."""
        if expected_reader is not None and self._reader is not expected_reader:
            return
        was_connected = self._writer is not None
        self._connection_stage = "disconnected"
        current_task = asyncio.current_task()
        reader_task = self._reader_task
        keepalive_task = self._keepalive_task
        writer = self._writer
        self._reader_task = None
        self._keepalive_task = None
        # Detach this transport before the first await. A new connection may be
        # established while the old reader/writer finishes closing.
        self._reader = None
        self._writer = None
        error = ConnectionError("BLE hub connection closed")
        for response_queue in list(self._pending.values()):
            response_queue.put_nowait(error)
        self._early_responses.clear()
        if was_connected:
            self._notify_connection_state(False)
        if writer is not None:
            writer.close()

        if (
            cancel_reader
            and reader_task is not None
            and reader_task is not current_task
        ):
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
        if (
            cancel_keepalive
            and keepalive_task is not None
            and keepalive_task is not current_task
        ):
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass

        if writer is not None:
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=_TIMEOUT)
            except Exception:  # noqa: BLE001 - closing is best-effort
                pass

    def _notify_connection_state(self, connected: bool) -> None:
        """Notify devices when their shared passive transport changes state."""
        for dev_id, listener in tuple(self._connection_listeners.items()):
            try:
                listener(connected)
            except Exception:  # noqa: BLE001 - isolate device callbacks
                _LOGGER.exception(
                    "Error applying BLE connection state for device %s",
                    hashlib.sha256(dev_id.encode()).hexdigest()[:12],
                )

    async def close(self) -> None:
        """Stop reconnecting and close the passive hub listener."""
        self._closing = True
        await self._stop_reconnect()
        await self._drop_connection()
        self._device_listeners.clear()
        self._connection_listeners.clear()

    async def _stop_reconnect(self) -> None:
        """Stop the background connector before changing settings or unloading."""
        reconnect_task = self._reconnect_task
        self._reconnect_task = None
        if (
            reconnect_task is not None
            and reconnect_task is not asyncio.current_task()
        ):
            reconnect_task.cancel()
            try:
                await reconnect_task
            except asyncio.CancelledError:
                pass

    def _record_connection_error(self, error: Exception) -> None:
        """Keep useful failure details without retaining exception text or secrets."""
        self._last_connection_error = {
            "stage": self._connection_stage,
            "type": type(error).__name__,
            "errno": error.errno if isinstance(error, OSError) else None,
            "time": datetime.now(UTC).isoformat(),
        }

    def device_diagnostics(self, dev_id: str) -> dict[str, Any]:
        """Return identifier-free passive listener diagnostics for one device."""
        last_hub_message = self._last_hub_message
        last_device_update = self._last_device_update.get(dev_id)
        return {
            "mode": "passive_hub_listener",
            "connected": self.connected,
            "registered": dev_id in self._device_listeners,
            "connectionStage": self._connection_stage,
            "connectionAttempts": self._connection_attempts,
            "lastConnectionError": self._last_connection_error,
            "commandConcurrencyLimit": _MAX_IN_FLIGHT,
            "pendingRequests": len(self._pending),
            "maxPendingRequests": self._max_pending_requests,
            "acknowledgedCommands": self._acknowledged_commands,
            "lastCommandLatencyMs": self._last_command_latency_ms,
            "lastSuccessfulLogin": (
                self._last_successful_login.isoformat()
                if self._last_successful_login else None
            ),
            "receivedFrameCount": self._received_frames,
            "unroutedUpdateCount": self._unrouted_updates,
            "deviceUpdateCount": self._device_update_counts.get(dev_id, 0),
            "lastHubMessage": (
                last_hub_message.isoformat() if last_hub_message else None
            ),
            "lastDeviceUpdate": (
                last_device_update.isoformat() if last_device_update else None
            ),
            "lastAttributeNames": list(
                self._last_device_attributes.get(dev_id, ())
            ),
        }

    @property
    def _anonymous_hub_id(self) -> str:
        return hashlib.sha256(self._hub_id.encode()).hexdigest()[:12]


_HUB_SESSIONS: dict[str, _HubSession] = {}


def _get_hub_session(
    hub_id: str, hub_ip: str, aes_key: bytes, user_id: str, hub_password: str
) -> _HubSession:
    """Return the shared session for a hub, creating it on first use."""
    session = _HUB_SESSIONS.get(hub_id)
    if session is None:
        session = _HubSession(hub_id, hub_ip, aes_key, user_id, hub_password)
        _HUB_SESSIONS[hub_id] = session
    return session


async def start_all_hub_sessions() -> None:
    """Start one passive listener for every registered BLE hub."""
    await asyncio.gather(
        *(
            session.start()
            for session in list(_HUB_SESSIONS.values())
            if session._device_listeners
        )
    )


async def close_all_hub_sessions() -> None:
    """Close every open hub connection and clear the registry (on unload)."""
    for session in list(_HUB_SESSIONS.values()):
        await session.close()
    _HUB_SESSIONS.clear()


class BleGatewayDeviceClient:
    """Control a BLE mesh light through the AiDot hub's TCP connection."""

    @property
    def hub_id(self) -> str:
        return self._hub_id

    def __init__(self, device: dict, hub_device: dict, user_id: str) -> None:
        self._device_id: str = device["id"]
        self._hub_id: str = hub_device["id"]
        self._hub_ip: str = hub_device.get("properties", {}).get("ipAddress", "")
        self._hub_password: str = hub_device.get("password", "")
        self._user_id: str = user_id
        self._diagnostic_id = hashlib.sha256(self._device_id.encode()).hexdigest()[:12]
        self._aes_key: bytes = _make_aes_key(
            (hub_device.get("aesKey") or [None])[0] or ""
        )
        self._diagnostic_secrets = _diagnostic_secret_values(
            {
                "device": device,
                "hub": hub_device,
                "userId": user_id,
            }
        )
        self._diagnostic_secrets.update(
            value
            for value in (device.get("name"), hub_device.get("name"))
            if isinstance(value, str) and value
        )
        self._diagnostic_attributes = _diagnostic_attributes(device)
        self._cloud_charging_supported = _advertises_charge_state(device)
        self._last_charging_report: datetime | None = None
        self._diagnostic_cloud_data = _redact_diagnostics(
            {
                "modelId": device.get("modelId"),
                "hardwareVersion": device.get("hardwareVersion"),
                "simpleVersion": device.get("simpleVersion"),
                "type": device.get("type"),
                "properties": device.get("properties", {}),
                "serviceModules": device.get("product", {}).get(
                    "serviceModules", []
                ),
            },
            self._diagnostic_secrets,
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
            effects=_effect_codes(device),
            cct_min=cct_min,
            cct_max=cct_max,
        )

        props = device.get("properties", {})
        raw_dim = int(props.get("Dimming", 100))
        raw_rgbw = int(props.get("RGBW", 0))
        rgbw_u = ctypes.c_uint32(raw_rgbw).value

        self.status = BleDeviceStatus(
            online=False,
            on=int(props.get("OnOff", 0)) == 1,
            dimming=int(raw_dim * 255 / 100),
            cct=int(props.get("CCT", cct_min)),
            rgbw=(
                (rgbw_u >> 24) & 0xFF,
                (rgbw_u >> 16) & 0xFF,
                (rgbw_u >> 8) & 0xFF,
                rgbw_u & 0xFF,
            ),
            battery=_parse_int(props.get("Battery_remaining"), 0, 100),
            mesh_rssi=_parse_int(props.get("meshNetRssi"), -127, 20),
            charging=(
                _parse_bool(props.get("chargeState"))
                if self._cloud_charging_supported else None
            ),
            energy_saving_enabled=_parse_bool(props.get("energySavingEnable")),
            energy_saving_factor=_parse_percentage_factor(
                props.get("energySavingFactor")
            ),
            light_duration=_parse_int(props.get("lightDuration"), 0, 86400),
            detection_mode=_parse_int(props.get("DetectionMode"), 0, 65535),
            security_alarm=_parse_alarm(props.get("SecurityAlarm")),
            alarm_status=_parse_alarm(props.get("alarmStatus")),
            effect_mode=_parse_int(props.get("EffectMode"), 0, 65535),
            active_color_mode={"RGBW": "rgbw", "CCT": "color_temp"}.get(
                props.get("lastNotifyCctRgbw")
            ),
        )
        self.on_status_update: Any = None
        self._hub_session = _get_hub_session(
            self._hub_id,
            self._hub_ip,
            self._aes_key,
            self._user_id,
            self._hub_password,
        )
        self._hub_session.register_device(
            self._device_id,
            self._handle_hub_update,
            self._handle_hub_connection,
        )

    async def async_update_hub(
        self,
        hub_device: dict[str, Any],
        user_id: str,
        on_login_failure: Callable[[], None] | None = None,
    ) -> None:
        """Apply refreshed hub credentials and gateway changes to this light."""
        self._diagnostic_secrets.update(_diagnostic_secret_values(hub_device))
        hub_id = hub_device["id"]
        hub_ip = hub_device.get("properties", {}).get("ipAddress", "")
        aes_key = _make_aes_key((hub_device.get("aesKey") or [None])[0] or "")
        password = hub_device.get("password", "")
        session = _get_hub_session(hub_id, hub_ip, aes_key, user_id, password)
        if session is not self._hub_session:
            old_session = self._hub_session
            old_session.unregister_device(self._device_id)
            await old_session.stop_if_unused()
            self._hub_session = session
            self._hub_id = hub_id
            session.register_device(
                self._device_id, self._handle_hub_update, self._handle_hub_connection
            )
        await session.async_update_connection(
            hub_ip, aes_key, user_id, password, on_login_failure
        )

    def update_cloud_properties(self, device: dict[str, Any]) -> None:
        """Apply cloud state as a low-frequency fallback without active polling."""
        props = device.get("properties", {})
        self._cloud_charging_supported = _advertises_charge_state(device)
        cloud_attributes = dict(props)
        # SL5C records include chargeState=0 without advertising this capability.
        # It is not evidence of discharge, and a cached value must not replace
        # a real charging report received through either live transport.
        if not self._cloud_charging_supported or self._last_charging_report:
            cloud_attributes.pop("chargeState", None)
            if self._last_charging_report is None:
                self.status.charging = None
        self._apply_attributes(cloud_attributes, record_freshness=False)
        self._diagnostic_attributes = _diagnostic_attributes(device)
        self._diagnostic_cloud_data["properties"] = _redact_diagnostics(
            props, self._diagnostic_secrets
        )
        if self.on_status_update:
            self.on_status_update(self.status)

    def supports_telemetry_attribute(self, attribute: str) -> bool:
        """Return whether cloud metadata advertises a telemetry attribute."""
        return attribute in self._diagnostic_attributes

    def _apply_telemetry_attributes(self, attributes: dict[str, Any]) -> bool:
        """Apply recognized live attributes and report whether any were valid."""
        received = False
        parsers = {
            "Battery_remaining": ("battery", lambda value: _parse_int(value, 0, 100)),
            "meshNetRssi": ("mesh_rssi", lambda value: _parse_int(value, -127, 20)),
            "chargeState": ("charging", _parse_bool),
            "energySavingEnable": ("energy_saving_enabled", _parse_bool),
            "energySavingFactor": (
                "energy_saving_factor",
                _parse_percentage_factor,
            ),
            "lightDuration": (
                "light_duration",
                lambda value: _parse_int(value, 0, 86400),
            ),
            "DetectionMode": (
                "detection_mode",
                lambda value: _parse_int(value, 0, 65535),
            ),
            "SecurityAlarm": ("security_alarm", _parse_alarm),
            "alarmStatus": ("alarm_status", _parse_alarm),
            "EffectMode": (
                "effect_mode",
                lambda value: _parse_int(value, 0, 65535),
            ),
        }
        for attribute, (status_key, parser) in parsers.items():
            if attribute not in attributes:
                continue
            value = parser(attributes[attribute])
            if value is None:
                continue
            setattr(self.status, status_key, value)
            received = True
        return received

    def _apply_attributes(
        self,
        attributes: dict[str, Any],
        *,
        record_freshness: bool,
    ) -> bool:
        """Apply control and telemetry attributes from one routed update."""
        received = self._apply_telemetry_attributes(attributes)

        if "OnOff" in attributes:
            value = _parse_bool(attributes["OnOff"])
            if value is not None:
                self.status.on = value
                received = True
        if "Dimming" in attributes:
            value = _parse_int(attributes["Dimming"], 0, 100)
            if value is not None:
                self.status.dimming = int(value * 255 / 100)
                received = True
        if "CCT" in attributes:
            value = _parse_int(attributes["CCT"], 0, 100000)
            if value is not None:
                self.status.cct = value
                if "RGBW" not in attributes:
                    self.status.active_color_mode = "color_temp"
                received = True
        if "RGBW" in attributes:
            try:
                raw_rgbw = int(attributes["RGBW"])
            except (TypeError, ValueError):
                pass
            else:
                if -(2**31) <= raw_rgbw <= (2**32 - 1):
                    packed = ctypes.c_uint32(raw_rgbw).value
                    self.status.rgbw = (
                        (packed >> 24) & 0xFF,
                        (packed >> 16) & 0xFF,
                        (packed >> 8) & 0xFF,
                        packed & 0xFF,
                    )
                    if "CCT" not in attributes:
                        self.status.active_color_mode = "rgbw"
                    received = True

        reported_mode = {"RGBW": "rgbw", "CCT": "color_temp"}.get(
            attributes.get("lastNotifyCctRgbw")
        )
        if reported_mode:
            self.status.active_color_mode = reported_mode
            received = True

        if received and record_freshness:
            self.status.online = True
            self.status.telemetry_connected = True
            self.status.last_telemetry_update = datetime.now(UTC)
            if _parse_bool(attributes.get("chargeState")) is not None:
                self._last_charging_report = self.status.last_telemetry_update
        return received

    @property
    def charging_diagnostics(self) -> dict[str, Any]:
        """Describe charging evidence independently of other light telemetry."""
        return {
            "state_source": (
                "device_report" if self._last_charging_report is not None
                else "cloud_snapshot" if self.status.charging is not None
                else "not_reported"
            ),
            "last_reported": (
                self._last_charging_report.isoformat()
                if self._last_charging_report is not None else None
            ),
            "cloud_attribute_advertised": self._cloud_charging_supported,
        }

    def _handle_hub_update(self, attributes: dict[str, Any]) -> None:
        """Apply an unsolicited update routed by the shared hub listener."""
        if not self._apply_attributes(attributes, record_freshness=True):
            return
        if self.on_status_update:
            self.on_status_update(self.status)

    def handle_cloud_notification(self, attributes: dict[str, Any]) -> None:
        """Apply an actual member notification routed from its known hub."""
        local_online = self.status.online
        if not self._apply_attributes(attributes, record_freshness=True):
            return
        # A cloud report proves fresh telemetry, not that HA can reach the
        # individual light's local control transport.
        self.status.online = local_online
        if self.on_status_update:
            self.on_status_update(self.status)

    async def async_run_group_command(self, operation: Any) -> Any:
        return await self._hub_session.run_group_command(operation)

    def _handle_hub_connection(self, connected: bool) -> None:
        """Expose whether the passive path to the powered hub is available."""
        self.status.online = connected
        self.status.telemetry_connected = connected
        if self.on_status_update:
            self.on_status_update(self.status)

    async def async_stop(self) -> None:
        """Unregister this device from the shared passive hub listener."""
        self._hub_session.unregister_device(self._device_id)
        await self._hub_session.stop_if_unused()

    async def async_get_diagnostics(self) -> dict[str, Any]:
        """Return redacted cloud data and passive listener health."""
        return {
            "transport": "ble_mesh_gateway",
            "anonymousDeviceId": self._diagnostic_id,
            "cloud": self._diagnostic_cloud_data,
            "requestedAttributes": self._diagnostic_attributes,
            "listener": self._hub_session.device_diagnostics(self._device_id),
            "charging": {"value": self.status.charging, **self.charging_diagnostics},
            "probe": {
                "status": "disabled_passive_mode",
                "reason": "Avoid waking battery-powered BLE devices",
            },
        }

    async def _send(self, attr: dict) -> None:
        await self._hub_session.send_command(self._device_id, attr)
        self._apply_attributes(attr, record_freshness=False)
        if self.on_status_update:
            self.on_status_update(self.status)

    async def send_dev_attr(self, attributes: dict[str, int]) -> None:
        """Match the Wi-Fi client's batch attribute API for compound commands."""
        await self._send(attributes)

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

    async def async_set_effect(self, effect: int) -> None:
        await self._send({"OnOff": 1, "EffectMode": effect})
