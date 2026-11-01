"""Account-scoped AiDot cloud group transport with bounded, single-shot writes."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientResponseError, ClientSession, ClientTimeout

from aidot.login_const import API_URL_TEMPLATE, APP_ID

from .group_inventory import GroupInventoryError, inspect_group
from .group_protocol import GroupProtocolError, build_group_request, group_response_code

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = 15
_RECONNECT_MIN = 5


class GroupUnavailable(ConnectionError):
    """The group transport cannot currently accept a command."""


def _new_mqtt_client(client_id: str) -> Any:
    """Import and construct the optional dependency away from HA's loop."""
    import paho.mqtt.client as mqtt

    return mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
        transport="websockets",
        clean_session=True,
        reconnect_on_failure=False,
    )


def mqtt_destination(config: dict, user_id: str) -> tuple[str, int]:
    """Validate the vendor's TLS destination before supplying credentials."""
    host = config.get("host")
    client_id = config.get("clientId")
    if not isinstance(host, str) or not isinstance(client_id, str):
        raise GroupProtocolError("Invalid MQTT configuration")
    url = urlsplit("wss://" + host + "/mqtt")
    if (
        not url.hostname
        or not url.hostname.endswith(".arnoo.com")
        or url.username is not None
        or url.password is not None
        or url.path != "/mqtt"
        or url.query
        or url.fragment
        or not client_id.endswith("-" + user_id)
        or not isinstance(config.get("password"), str)
    ):
        raise GroupProtocolError("Invalid MQTT destination or account")
    return url.hostname, url.port or 443


class GroupCloudClient:
    """Own one WebPC session; marshal MQTT callbacks onto the asyncio loop."""

    def __init__(
        self,
        session: ClientSession,
        login: Callable[[], dict],
        recover_auth: Callable[[str], Awaitable[None]],
        *,
        on_message: Callable[[str, dict], None],
        on_connection: Callable[[bool], None],
        mqtt_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._http = session
        self._login = login
        self._recover_auth = recover_auth
        self._on_message = on_message
        self._on_connection = on_connection
        self._mqtt_factory = mqtt_factory
        self._loop = asyncio.get_running_loop()
        self._session_id = uuid.uuid4().hex
        self._hubs: set[str] = set()
        self._has_hubs = asyncio.Event()
        self._reset = asyncio.Event()
        self._worker: asyncio.Task | None = None
        self._client: Any = None
        self._client_id = ""
        self._user_id = ""
        self._connected_token = ""
        self._ready: asyncio.Future | None = None
        self._pending: dict[str, tuple[dict, asyncio.Future, float]] = {}
        self._own_sequences: deque[str] = deque(maxlen=512)
        self._slots = asyncio.Semaphore(4)
        self._closed = False
        self.connected = False
        self.diagnostics: dict[str, Any] = {
            "connected": False,
            "connectionAttempts": 0,
            "lastErrorType": None,
            "lastAcknowledgementMs": None,
            "lastAcknowledgement": None,
            "commandsSent": 0,
            "notificationCount": 0,
            "telemetrySubscriptionsAccepted": False,
        }

    def set_hubs(self, hubs: set[str]) -> None:
        """Refresh subscriptions only when the eligible hub set changes."""
        if hubs == self._hubs:
            return
        self._hubs = set(hubs)
        if hubs:
            self._has_hubs.set()
        else:
            self._has_hubs.clear()
        self._reset.set()

    def start(self) -> None:
        if self._worker is None and not self._closed:
            self._worker = self._loop.create_task(self._maintain())

    def token_updated(self) -> None:
        if (
            self._client is not None
            and self._login().get("accessToken") != self._connected_token
        ):
            self._reset.set()

    async def _get(
        self, path: str, *, web: bool = False, params: dict | None = None
    ) -> Any:
        for attempt in range(2):
            login = self._login()
            region = login.get("region", "").lower()
            if region not in {"us", "eu", "cn", "jp"}:
                raise GroupProtocolError("Unsupported AiDot account region")
            token = login.get("accessToken", "")
            headers = {"appId": APP_ID, "terminal": "app", "token": token}
            if web:
                headers.update(
                    {"owner": login["id"], "webVersion": "0.5.5", "locale": "en-US"}
                )
                base = f"https://prod-{region}-api.arnoo.com/v29"
            else:
                base = API_URL_TEMPLATE.format(region=region)
            try:
                async with self._http.get(
                    base + path,
                    headers=headers,
                    params=params,
                    timeout=ClientTimeout(total=20),
                    allow_redirects=False,
                ) as response:
                    response.raise_for_status()
                    if 300 <= response.status < 400:
                        raise GroupProtocolError("Unexpected AiDot API redirect")
                    return await response.json()
            except ClientResponseError as error:
                if error.status != 401 or attempt:
                    raise
                await self._recover_auth(token)
        raise GroupUnavailable("AiDot authentication failed")

    async def discover(self, devices: list[dict]) -> tuple[list[tuple[str, dict]], int]:
        """Read owned-home groups; resolve every member against current devices."""
        houses = await self._get("/houses")
        if not isinstance(houses, list):
            raise GroupProtocolError("Unexpected owned-home response")
        targets: list[tuple[str, dict]] = []
        excluded = 0
        seen: set[tuple[str, str]] = set()
        for house in houses:
            if not isinstance(house, dict) or house.get("isOwner") is not True:
                continue
            home_id = house.get("id")
            if not isinstance(home_id, str) or not home_id:
                raise GroupProtocolError("Owned home has no identifier")
            groups = await self._get("/groups", params={"houseId": home_id})
            if not isinstance(groups, list):
                raise GroupProtocolError("Unexpected group response")
            for group in groups:
                try:
                    if not isinstance(group, dict):
                        raise GroupInventoryError("Invalid group")
                    inspect_group(group, devices)
                except GroupInventoryError:
                    excluded += 1
                    continue
                identity = (home_id, group["id"])
                if identity in seen:
                    raise GroupProtocolError("Duplicate group identity")
                seen.add(identity)
                targets.append((home_id, group))
        return targets, excluded

    async def _maintain(self) -> None:
        delay = _RECONNECT_MIN
        try:
            while not self._closed:
                await self._has_hubs.wait()
                self._reset.clear()
                try:
                    await self._connect()
                    delay = _RECONNECT_MIN
                    await self._reset.wait()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - isolate optional cloud groups
                    self.diagnostics["lastErrorType"] = type(error).__name__
                    _LOGGER.debug(
                        "AiDot group connection failed: %s", type(error).__name__
                    )
                finally:
                    await self._dispose()
                if not self._closed:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 120)
        finally:
            await self._dispose()

    async def _connect(self) -> None:
        self.diagnostics["connectionAttempts"] += 1
        config = await self._get(
            "/commons/mqttConfig",
            web=True,
            params={"source": "WebPC", "sessionId": self._session_id},
        )
        if isinstance(config, dict) and isinstance(config.get("data"), dict):
            config = config["data"]
        if not isinstance(config, dict):
            raise GroupProtocolError("Unexpected MQTT configuration")
        login = self._login()
        host, port = mqtt_destination(config, login["id"])
        client = await asyncio.to_thread(
            self._mqtt_factory or _new_mqtt_client, config["clientId"]
        )
        self._client = client
        self._client_id = config["clientId"]
        self._user_id = login["id"]
        self._connected_token = login["accessToken"]
        self._ready = self._loop.create_future()
        client.connect_timeout = 10
        client.username_pw_set(self._user_id, config["password"])
        client.tls_set_context(await asyncio.to_thread(ssl.create_default_context))
        client.ws_set_options(path="/mqtt")
        client.on_connect = lambda c, u, f, r, p: self._call(self._connected, c, r)
        client.on_subscribe = lambda c, u, m, r, p: self._call(self._subscribed, c, r)
        client.on_disconnect = lambda c, u, f, r, p: self._call(self._disconnected, c)
        client.on_message = self._mqtt_message
        heartbeat = max(5, min(int(config.get("heartbeat", 45)), 120))
        connecting = self._loop.create_task(
            asyncio.to_thread(client.connect, host, port, heartbeat)
        )
        try:
            await asyncio.shield(connecting)
        except asyncio.CancelledError:
            # Let the bounded socket connect finish before disposal, so it
            # cannot open a new socket after the entry has finished unloading.
            with suppress(OSError, ValueError):
                await connecting
            raise
        client.loop_start()
        await asyncio.wait_for(asyncio.shield(self._ready), _TIMEOUT)

    def _call(self, callback: Callable, *args: Any) -> None:
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(callback, *args)

    def _connected(self, client: Any, reason: Any) -> None:
        if client is not self._client:
            return
        if getattr(reason, "value", reason) != 0:
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(
                    GroupUnavailable("AiDot rejected the cloud connection")
                )
            return
        topics = [(f"iot/v1/c/{self._user_id}/#", 0)]
        topics += [(f"iot/v1/cb/{hub}/#", 0) for hub in sorted(self._hubs)]
        client.subscribe(topics)

    def _subscribed(self, client: Any, reasons: list[Any]) -> None:
        if client is not self._client or self._ready is None or self._ready.done():
            return
        codes = [getattr(reason, "value", reason) for reason in reasons]
        if not codes or codes[0] != 0:
            self._ready.set_exception(GroupUnavailable("AiDot denied group replies"))
            return
        self.diagnostics["telemetrySubscriptionsAccepted"] = all(
            code == 0 for code in codes[1:]
        )
        self.diagnostics["lastErrorType"] = None
        self._set_connected(True)
        self._ready.set_result(None)

    def _disconnected(self, client: Any) -> None:
        if client is not self._client:
            return
        self._set_connected(False)
        self._fail_pending()
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(GroupUnavailable("AiDot cloud connection closed"))
        self._reset.set()

    def _set_connected(self, connected: bool) -> None:
        changed = self.connected != connected
        self.connected = self.diagnostics["connected"] = connected
        if changed:
            self._on_connection(connected)

    def _mqtt_message(self, client: Any, userdata: Any, message: Any) -> None:
        if len(message.payload) > 65536:
            return
        try:
            data = json.loads(message.payload)
        except (ValueError, RecursionError):
            return
        if isinstance(data, dict):
            self._call(self._message, client, message.topic, data)

    def _message(self, client: Any, topic: str, message: dict) -> None:
        if client is not self._client:
            return
        seq = message.get("seq")
        pending = self._pending.get(seq) if isinstance(seq, str) else None
        if pending is not None:
            request, future, sent = pending
            try:
                code = group_response_code(
                    topic=topic,
                    message=message,
                    request=request,
                    user_id=self._user_id,
                )
            except GroupProtocolError as error:
                if not future.done():
                    future.set_exception(error)
                return
            if code == 200:
                elapsed = round((time.monotonic() - sent) * 1000, 1)
                self.diagnostics["lastAcknowledgementMs"] = elapsed
                self.diagnostics["lastAcknowledgement"] = datetime.now(UTC).isoformat()
                if not future.done():
                    future.set_result(elapsed)
                return
        if isinstance(seq, str) and seq in self._own_sequences:
            return
        self.diagnostics["notificationCount"] += 1
        self._on_message(topic, message)

    async def send(
        self, home_id: str, group: dict, devices: list[dict], attributes: dict
    ) -> float:
        """Send once, wait for a matching application reply, never replay."""
        async with self._slots:
            if not self.connected or self._client is None:
                raise GroupUnavailable("AiDot group cloud connection is unavailable")
            topic, request = build_group_request(
                group=group,
                devices=devices,
                home_id=home_id,
                user_id=self._user_id,
                client_id=self._client_id,
                attributes=attributes,
                sequence=uuid.uuid4().hex,
                unix_time=int(time.time()),
            )
            future = self._loop.create_future()
            seq = request["seq"]
            self._own_sequences.append(seq)
            self._pending[seq] = (request, future, time.monotonic())
            try:
                # QoS 0 plus a correlated application reply avoids replaying
                # queued writes after a reconnect. Never retain light commands.
                info = self._client.publish(
                    topic, json.dumps(request), qos=0, retain=False
                )
                if info.rc != 0:
                    raise GroupUnavailable("AiDot group request could not be sent")
                self.diagnostics["commandsSent"] += 1
                return await asyncio.wait_for(future, _TIMEOUT)
            finally:
                self._pending.pop(seq, None)
                if not future.done():
                    future.cancel()

    def _fail_pending(self) -> None:
        for _, future, _ in self._pending.values():
            if not future.done():
                future.set_exception(GroupUnavailable("AiDot cloud connection closed"))
        self._pending.clear()

    async def _dispose(self) -> None:
        client, self._client = self._client, None
        self._set_connected(False)
        self._fail_pending()
        if self._ready is not None:
            if not self._ready.done():
                self._ready.cancel()
            elif not self._ready.cancelled():
                self._ready.exception()
        if client is not None:
            try:
                await asyncio.to_thread(client.disconnect)
            finally:
                await asyncio.to_thread(client.loop_stop)

    async def close(self) -> None:
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        else:
            await self._dispose()
