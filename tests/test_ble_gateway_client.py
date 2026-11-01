"""Transport-layer tests for ble_gateway_client.

These exercise the per-hub session: a shared lock (serialises the 8 entities
that share one hub), connection reuse (login once, reuse the socket), and retry
on transient connection errors. The socket layer is faked; the AES framing is
real (round-tripped through the installed ``aidot`` library).
"""

import asyncio
import json
import struct

import pytest

import ble_gateway_client as bgc


# --- fakes -----------------------------------------------------------------


class _Tracker:
    """Counts max concurrent entries to detect interleaving."""

    def __init__(self) -> None:
        self.active = 0
        self.max = 0

    def enter(self) -> None:
        self.active += 1
        self.max = max(self.max, self.active)

    def exit(self) -> None:
        self.active -= 1


class FakeWriter:
    def __init__(self, tracker: _Tracker | None = None) -> None:
        self.tracker = tracker
        self.writes: list[bytes] = []
        self._closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        # Yield control so a second, unsynchronised command could interleave.
        if self.tracker is not None:
            self.tracker.enter()
            await asyncio.sleep(0)
            self.tracker.exit()
        else:
            await asyncio.sleep(0)

    def close(self) -> None:
        self._closed = True

    def is_closing(self) -> bool:
        return self._closed

    async def wait_closed(self) -> None:
        pass


class FakeReader:
    """Serve framed bytes, then remain open unless an error was requested."""

    def __init__(self, frames: list[bytes], exc: Exception | None = None) -> None:
        self._buf = b"".join(frames)
        self._pos = 0
        self._exc = exc

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) - self._pos < n:
            if self._exc is not None:
                raise self._exc
            await asyncio.Future()
        chunk = self._buf[self._pos : self._pos + n]
        self._pos += n
        return chunk


class FakeServer:
    """Hands out one (reader, writer) per ``open_connection`` call."""

    def __init__(self, conn_specs: list[dict]) -> None:
        # Each spec: {"frames": [...], "exc": Exception|None, "tracker": Tracker|None}
        self._specs = conn_specs
        self.connect_count = 0
        self.writers: list[FakeWriter] = []
        self.readers: list[FakeReader] = []

    async def open_connection(self, host, port):
        spec = self._specs[min(self.connect_count, len(self._specs) - 1)]
        self.connect_count += 1
        reader = FakeReader(spec.get("frames", []), spec.get("exc"))
        writer = FakeWriter(spec.get("tracker"))
        self.readers.append(reader)
        self.writers.append(writer)
        return reader, writer


# --- helpers ---------------------------------------------------------------

KEY = bgc._make_aes_key("testkey")


def _frame_obj(obj: dict) -> bytes:
    return bgc._frame(json.dumps(obj).encode(), KEY)


def _login_frame(asc: int = 5) -> bytes:
    return _frame_obj({"payload": {"ascNumber": asc}})


def _ack_frame(seq: str = "ha9300001") -> bytes:
    return _frame_obj({"seq": seq, "payload": {}})


def _attr_frame(
    attr: dict,
    *,
    dev_id: str = "devA",
    method: str = "devAttrNotify",
    seq: str | None = None,
) -> bytes:
    response = {
        "method": method,
        "deviceId": dev_id,
        "payload": {"devId": dev_id, "attr": attr},
    }
    if seq is not None:
        response["seq"] = seq
    return _frame_obj(response)


def _session() -> "bgc._HubSession":
    return bgc._HubSession(
        hub_id="hub1",
        hub_ip="10.0.0.5",
        aes_key=KEY,
        user_id="user1",
        hub_password="pw",
    )


def _count_logins(writer: FakeWriter) -> int:
    """Decode written frames and count loginReq messages."""
    count = 0
    for data in writer.writes:
        body = data[8:]  # strip 8-byte header
        try:
            msg = json.loads(bgc._aes_decrypt(body, KEY))
        except Exception:
            continue
        if msg.get("method") == "loginReq":
            count += 1
    return count


@pytest.fixture(autouse=True)
async def _clear_registry():
    bgc._HUB_SESSIONS.clear()
    bgc._seq = 0
    yield
    await bgc.close_all_hub_sessions()


# --- tests -----------------------------------------------------------------


async def test_concurrent_commands_serialise(monkeypatch):
    """Two commands racing on the same hub must not interleave (shared lock)."""
    tracker = _Tracker()
    # One connection, reused; enough acks for both commands.
    server = FakeServer(
        [{
            "frames": [
                _login_frame(),
                _ack_frame("ha9300001"),
                _ack_frame("ha9300002"),
            ],
            "tracker": tracker,
        }]
    )
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    sess = _session()
    await asyncio.gather(
        sess.send_command("devA", {"OnOff": 1}),
        sess.send_command("devB", {"OnOff": 0}),
    )

    assert tracker.max == 1, "commands interleaved — lock not held across the call"


async def test_connection_is_reused_login_once(monkeypatch):
    """Sequential commands reuse the socket and log in exactly once."""
    server = FakeServer(
        [{
            "frames": [
                _login_frame(),
                _ack_frame("ha9300001"),
                _ack_frame("ha9300002"),
            ]
        }]
    )
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    sess = _session()
    await sess.send_command("devA", {"OnOff": 1})
    await sess.send_command("devA", {"OnOff": 0})

    assert server.connect_count == 1, "opened a new connection instead of reusing"
    assert _count_logins(server.writers[0]) == 1, "logged in more than once"


async def test_ascnumber_increments_per_command(monkeypatch):
    """Reused connection bumps ascNumber per command (login asc was 5)."""
    server = FakeServer(
        [{
            "frames": [
                _login_frame(asc=5),
                _ack_frame("ha9300001"),
                _ack_frame("ha9300002"),
            ]
        }]
    )
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    sess = _session()
    await sess.send_command("devA", {"OnOff": 1})
    await sess.send_command("devA", {"OnOff": 0})

    ascs = []
    for data in server.writers[0].writes:
        msg = json.loads(bgc._aes_decrypt(data[8:], KEY))
        if msg.get("method") == "setDevAttrReq":
            ascs.append(msg["payload"]["ascNumber"])
    assert ascs == [6, 7], f"expected ascNumber 6,7 got {ascs}"


async def test_get_attributes_uses_ble_channel_and_returns_telemetry(monkeypatch):
    """An explicit read is correlated while the passive connection stays open."""
    server = FakeServer(
        [{
            "frames": [
                _login_frame(),
                _attr_frame(
                    {"Battery": 87, "RSSI": -61}, seq="ha9300001"
                ),
            ]
        }]
    )
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    sess = _session()
    responses = await sess.get_attributes("devA", ["Battery", "RSSI"])

    messages = [
        json.loads(bgc._aes_decrypt(data[8:], KEY))
        for data in server.writers[0].writes
    ]
    request = next(msg for msg in messages if msg.get("method") == "getDevAttrReq")
    assert request["payload"]["channel"] == "ble"
    assert request["payload"]["attr"] == ["Battery", "RSSI"]
    assert responses[0]["payload"]["attr"] == {"Battery": 87, "RSSI": -61}
    assert not server.writers[0].is_closing()


async def test_get_attributes_collects_ack_and_followup_telemetry(monkeypatch):
    """The hub may acknowledge first and send the attribute payload second."""
    server = FakeServer(
        [
            {
                "frames": [
                    _login_frame(),
                    _ack_frame("ha9300001"),
                    _attr_frame({"Battery": 54}, seq="ha9300001"),
                ]
            }
        ]
    )
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    responses = await _session().get_attributes("devA", ["Battery"])

    assert len(responses) == 2
    assert responses[1]["payload"]["attr"]["Battery"] == 54


async def test_unsolicited_update_does_not_complete_another_device_request(
    monkeypatch,
):
    """An interleaved push is routed without stealing a command response."""
    server = FakeServer([
        {
            "frames": [
                _login_frame(),
                _attr_frame({"OnOff": 1}, dev_id="devB"),
                _ack_frame("ha9300001"),
            ]
        }
    ])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    client_a = _make_client("devA", "Spot A")
    client_b = _make_client("devB", "Spot B")
    await client_a.async_turn_on()

    assert client_a.status.on is True
    assert client_b.status.on is True


async def test_retry_reconnects_after_reset(monkeypatch):
    """A transient reset on the first attempt triggers reconnect + relogin."""
    monkeypatch.setattr(bgc, "_BACKOFF", 0)
    server = FakeServer(
        [
            {"frames": [_login_frame()], "exc": ConnectionResetError(104, "reset")},
            {"frames": [_login_frame(), _ack_frame("ha9300002")]},
        ]
    )
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    sess = _session()
    await sess.send_command("devA", {"OnOff": 1})

    assert server.connect_count == 2, "did not reconnect after reset"


async def test_retry_gives_up_after_max_attempts(monkeypatch):
    """Persistent failure raises after _MAX_RETRIES attempts."""
    monkeypatch.setattr(bgc, "_BACKOFF", 0)
    server = FakeServer(
        [{"frames": [_login_frame()], "exc": ConnectionResetError(104, "reset")}]
    )
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    sess = _session()
    with pytest.raises(ConnectionError):
        await sess.send_command("devA", {"OnOff": 1})

    assert server.connect_count == bgc._MAX_RETRIES


async def test_close_all_hub_sessions_clears_registry(monkeypatch):
    """close_all_hub_sessions() closes live writers and empties the registry."""
    server = FakeServer([{"frames": [_login_frame(), _ack_frame()]}])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    client = _make_client()
    await client.async_turn_on()
    assert bgc._HUB_SESSIONS  # session was created

    await bgc.close_all_hub_sessions()

    assert not bgc._HUB_SESSIONS
    assert server.writers[0].is_closing()


async def test_passive_listener_reconnects_and_resumes_routing(monkeypatch):
    """A dropped hub socket reconnects without polling any BLE device."""
    monkeypatch.setattr(bgc, "_BACKOFF", 0)
    server = FakeServer([
        {
            "frames": [_login_frame(), _attr_frame({"OnOff": 1})],
            "exc": ConnectionResetError(104, "reset"),
        },
        {"frames": [_login_frame(), _attr_frame({"OnOff": 0})]},
    ])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    client = _make_client()
    await bgc.start_all_hub_sessions()
    for _ in range(20):
        diagnostics = client._hub_session.device_diagnostics("devA")
        if diagnostics["deviceUpdateCount"] == 2:
            break
        await asyncio.sleep(0)

    assert server.connect_count == 2
    assert diagnostics["deviceUpdateCount"] == 2
    assert client.status.on is False
    assert all(
        json.loads(bgc._aes_decrypt(data[8:], KEY))["method"] == "loginReq"
        for writer in server.writers
        for data in writer.writes
    )


async def test_keepalive_targets_only_the_powered_hub(monkeypatch):
    """The listener keepalive contains no BLE device query or identifier."""
    monkeypatch.setattr(bgc, "_HUB_KEEPALIVE_INTERVAL", 0.001)
    server = FakeServer([{"frames": [_login_frame()]}])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    _make_client()
    await bgc.start_all_hub_sessions()
    await asyncio.sleep(0.005)

    framed_messages = [
        (
            struct.unpack(">HhI", data[:8])[1],
            json.loads(bgc._aes_decrypt(data[8:], KEY)),
        )
        for data in server.writers[0].writes
    ]
    pings = [message for message_type, message in framed_messages if message_type == 2]
    assert pings
    assert all(ping["method"] == "pingreq" for ping in pings)
    assert all(ping["service"] == "test" for ping in pings)
    assert all(ping["payload"] == {} for ping in pings)
    assert all("deviceId" not in ping for ping in pings)
    assert all(
        message["method"] != "getDevAttrReq"
        for _, message in framed_messages
    )


async def test_hub_disconnect_and_recovery_update_all_light_availability(monkeypatch):
    server = FakeServer([{"frames": [_login_frame()]}])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)
    clients = [_make_client("devA"), _make_client("devB")]
    session = clients[0]._hub_session
    assert all(client.status.online is False for client in clients)

    await session.start()
    assert all(client.status.online is True for client in clients)
    await session._drop_connection()
    assert all(client.status.online is False for client in clients)
    assert all(client.status.telemetry_connected is False for client in clients)

    await session.start()
    assert all(client.status.online is True for client in clients)
    assert all(client.status.last_telemetry_update is None for client in clients)


async def test_refreshed_hub_credentials_reconnect_shared_session_in_place(monkeypatch):
    new_key = bgc._make_aes_key("replacementkey")
    new_login = bgc._frame(json.dumps({"payload": {"ascNumber": 8}}).encode(), new_key)
    new_ack = bgc._frame(json.dumps({"seq": "ha9300001", "payload": {}}).encode(), new_key)
    server = FakeServer([
        {"frames": [_login_frame()]},
        {"frames": [new_login, new_ack]},
    ])
    hosts = []

    async def connect(host, port):
        hosts.append(host)
        return await server.open_connection(host, port)

    monkeypatch.setattr(bgc.asyncio, "open_connection", connect)
    client_a, client_b = _make_client("devA"), _make_client("devB")
    session = client_a._hub_session
    await session.start()
    updated_hub = {
        "id": "hub1", "password": "new-password", "aesKey": ["replacementkey"],
        "properties": {"ipAddress": "10.0.0.9"},
    }
    await client_a.async_update_hub(updated_hub, "new-user")
    await client_b.async_update_hub(updated_hub, "new-user")
    assert server.writers[0].is_closing()
    assert client_a._hub_session is client_b._hub_session is session
    assert all(not client.status.online for client in (client_a, client_b))

    await client_b.async_turn_on()
    assert hosts == ["10.0.0.5", "10.0.0.9"]
    login = json.loads(bgc._aes_decrypt(server.writers[1].writes[0][8:], new_key))
    assert login["payload"]["password"] == "new-password"
    assert login["payload"]["userId"] == "new-user"
    assert all(client.status.online for client in (client_a, client_b))
    assert set(session._device_listeners) == {"devA", "devB"}

    await client_a.async_update_hub(updated_hub, "new-user")
    assert session.connected
    assert server.connect_count == 2


async def test_reset_during_login_records_phase_and_requests_refresh(monkeypatch):
    server = FakeServer([{"exc": ConnectionResetError(104, "secret-password")}])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)
    client = _make_client()
    refreshes = []
    client._hub_session._on_login_failure = lambda: refreshes.append(True)
    with pytest.raises(ConnectionResetError):
        await client._hub_session._ensure_connected()
    diagnostics = client._hub_session.device_diagnostics("devA")
    assert diagnostics["lastConnectionError"]["stage"] == "login_response"
    assert diagnostics["lastConnectionError"]["errno"] == 104
    assert diagnostics["connectionAttempts"] == 1
    assert "secret-password" not in json.dumps(diagnostics)
    assert refreshes == [True]
    assert not client.status.online


async def test_silent_hub_is_disconnected_and_retried(monkeypatch):
    monkeypatch.setattr(bgc, "_HUB_IDLE_TIMEOUT", 0.005)
    monkeypatch.setattr(bgc, "_BACKOFF", 0.05)
    server = FakeServer([{"frames": [_login_frame()]}])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)
    client = _make_client()
    await client._hub_session.start()
    assert client.status.online
    await asyncio.sleep(0.015)
    # The retry may already be connected, but the silent socket must be closed.
    assert server.writers[0].is_closing()
    assert client._hub_session.device_diagnostics("devA")["lastConnectionError"]["type"] == "TimeoutError"


async def test_gateway_change_unregisters_old_hub_and_keeps_new_listener():
    client = _make_client()
    old_session = client._hub_session
    await client.async_update_hub(
        {"id": "hub2", "password": "pw", "aesKey": ["testkey"],
         "properties": {"ipAddress": "10.0.0.8"}},
        "user1",
    )
    assert old_session._closing
    assert not old_session._device_listeners
    assert "hub1" not in bgc._HUB_SESSIONS
    assert client._hub_session is bgc._HUB_SESSIONS["hub2"]
    assert "devA" in client._hub_session._device_listeners


async def test_compound_attributes_use_one_ble_packet(monkeypatch):
    server = FakeServer([{"frames": [_login_frame(), _ack_frame()]}])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)
    client = _make_client()
    await client.send_dev_attr({"OnOff": 1, "RGBW": -16777216, "Dimming": 50})
    messages = [json.loads(bgc._aes_decrypt(w[8:], KEY)) for w in server.writers[0].writes]
    commands = [m for m in messages if m["method"] == "setDevAttrReq"]
    assert len(commands) == 1
    assert commands[0]["payload"]["attr"] == {"OnOff": 1, "RGBW": -16777216, "Dimming": 50}
    assert client.status.rgbw == (255, 0, 0, 0)
    assert client.status.dimming == 127
    assert client.status.active_color_mode == "rgbw"


async def test_rejected_command_does_not_update_state_or_reconnect(monkeypatch):
    reject = _frame_obj({"seq": "ha9300001", "ack": {"code": 403}, "payload": {}})
    server = FakeServer([{"frames": [_login_frame(), reject]}])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)
    client = _make_client()
    with pytest.raises(bgc.HubCommandError, match="403"):
        await client.send_dev_attr({"OnOff": 1, "RGBW": -16777216, "Dimming": 50})
    assert client.status.on is False
    assert server.connect_count == 1
    assert client._hub_session.connected


def test_colour_mode_follows_unambiguous_telemetry():
    client = _make_client()
    client._handle_hub_update({"CCT": 2700})
    assert client.status.active_color_mode == "color_temp"
    client._handle_hub_update({"RGBW": -16777216})
    assert client.status.active_color_mode == "rgbw"
    client._handle_hub_update({"CCT": 3000, "RGBW": -16777216})
    assert client.status.active_color_mode == "rgbw"
    client._handle_hub_update({"lastNotifyCctRgbw": "CCT"})
    assert client.status.active_color_mode == "color_temp"


class ControlledServer:
    """Supply acknowledgements only when the test releases them."""

    def __init__(self):
        self.writers = []
        self.commands = []
        self.auto_ack = False

    async def open_connection(self, host, port):
        server = self
        reader = asyncio.StreamReader()

        class Writer(FakeWriter):
            def write(self, data):
                super().write(data)
                message = json.loads(bgc._aes_decrypt(data[8:], KEY))
                if message["method"] == "loginReq":
                    reader.feed_data(_login_frame())
                else:
                    server.commands.append((reader, message))
                    if server.auto_ack:
                        server.ack(reader, message)

            def close(self):
                super().close()
                reader.feed_eof()

        writer = Writer()
        self.writers.append(writer)
        return reader, writer

    def ack(self, reader, message):
        reader.feed_data(_frame_obj({
            "seq": message["seq"], "ack": {"code": 200},
            "payload": {"devId": message["payload"]["devId"]},
        }))


async def wait_for_commands(server, count):
    for _ in range(200):
        if len(server.commands) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"Expected {count} commands, got {len(server.commands)}")


async def test_pipeline_is_bounded_and_correlates_reversed_replies(monkeypatch):
    server = ControlledServer()
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)
    session = _session()
    tasks = [asyncio.create_task(session._request(
        "setDevAttrReq", f"dev{i}", {"OnOff": 1}
    )) for i in range(8)]
    try:
        await wait_for_commands(server, 4)
        assert len(server.commands) == 4
        assert len(session._pending) == 4
        for reader, message in reversed(server.commands[:4]):
            server.ack(reader, message)
        await wait_for_commands(server, 8)
        for reader, message in reversed(server.commands[4:]):
            server.ack(reader, message)
        results = await asyncio.gather(*tasks)
        assert [r[0]["payload"]["devId"] for r in results] == [f"dev{i}" for i in range(8)]
        assert len(server.writers) == 1
        assert session._max_pending_requests == 4
        assert session._acknowledged_commands == 8
    finally:
        await session.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_pipeline_preserves_same_light_command_order(monkeypatch):
    server = ControlledServer()
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)
    session = _session()
    tasks = [asyncio.create_task(session.send_command(dev, {"OnOff": on}))
             for dev, on in [("devA", 1), ("devA", 0), ("devB", 1)]]
    try:
        await wait_for_commands(server, 2)
        assert {m["payload"]["devId"] for _, m in server.commands} == {"devA", "devB"}
        for reader, message in server.commands[:2]:
            server.ack(reader, message)
        await wait_for_commands(server, 3)
        reader, message = server.commands[2]
        assert message["payload"]["devId"] == "devA"
        assert message["payload"]["attr"] == {"OnOff": 0}
        server.ack(reader, message)
        await asyncio.gather(*tasks)
    finally:
        await session.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_pending_commands_share_reconnect_without_closing_new_socket(monkeypatch):
    monkeypatch.setattr(bgc, "_BACKOFF", 0)
    server = ControlledServer()
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)
    session = _session()
    tasks = [asyncio.create_task(session.send_command(dev, {"OnOff": 1}))
             for dev in ("devA", "devB")]
    try:
        await wait_for_commands(server, 2)
        old_reader = session._reader
        server.auto_ack = True
        await session._drop_connection()
        await asyncio.gather(*tasks)
        assert len(server.writers) == 2
        assert server.writers[0].is_closing()
        await session._drop_connection(expected_reader=old_reader)
        assert session.connected
        assert not server.writers[1].is_closing()
        assert not session._pending
    finally:
        await session.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_response_device_id_prefers_light_source_over_hub_device_id() -> None:
    """A source address can route a notification whose deviceId is the hub."""
    assert (
        bgc._HubSession._response_device_id(
            {"srcAddr": "1.devA", "deviceId": "hub1"}, {}
        )
        == "devA"
    )


# --- behaviour preserved through the client (regression guard) -------------


def _make_client(
    dev_id: str = "devA", name: str = "Spot 1"
) -> "bgc.BleGatewayDeviceClient":
    device = {
        "id": dev_id,
        "mac": "AA:BB",
        "name": name,
        "product": {"serviceModules": []},
        "properties": {},
    }
    hub = {
        "id": "hub1",
        "password": "pw",
        "aesKey": ["testkey"],
        "properties": {"ipAddress": "10.0.0.5"},
    }
    return bgc.BleGatewayDeviceClient(device, hub, "user1")


def _make_diagnostic_client() -> "bgc.BleGatewayDeviceClient":
    device = {
        "id": "secret-device-id",
        "mac": "AA:BB:CC:DD:EE:FF",
        "name": "Back Garden",
        "modelId": "LK.A000181",
        "hardwareVersion": "1.0",
        "type": "light",
        "bleMeshDeviceKey": "secret-mesh-key",
        "product": {
            "serviceModules": [
                {
                    "identity": "sensor.battery",
                    "name": "1",
                    "properties": [
                        {
                            "identity": "Battery_remaining",
                            "dataType": "int",
                            "minValue": 0,
                            "maxValue": 100,
                        }
                    ],
                },
                {
                    "identity": "diagnostic.signal",
                    "properties": [
                        {"identity": "meshNetRssi", "dataType": "int"}
                    ],
                },
            ]
        },
        "properties": {
            "OnOff": 0,
            "Battery_remaining": 86,
            "chargeState": "1",
            "ipAddress": "192.0.2.10",
            "ssidName": "Private WiFi",
        },
    }
    hub = {
        "id": "secret-hub-id",
        "password": "secret-password",
        "aesKey": ["testkey"],
        "properties": {"ipAddress": "10.0.0.5"},
    }
    return bgc.BleGatewayDeviceClient(device, hub, "secret-user-id")


async def test_diagnostics_are_redacted_without_active_probe():
    """Diagnostics expose listener health without waking a BLE light."""
    diagnostics = await _make_diagnostic_client().async_get_diagnostics()
    encoded = json.dumps(diagnostics)

    assert diagnostics["requestedAttributes"] == [
        "Battery_remaining",
        "OnOff",
        "chargeState",
        "meshNetRssi",
    ]
    assert len(diagnostics["anonymousDeviceId"]) == 12
    assert diagnostics["cloud"]["properties"]["Battery_remaining"] == 86
    assert diagnostics["cloud"]["properties"]["chargeState"] == "1"
    assert diagnostics["cloud"]["serviceModules"][0]["identity"] == "sensor.battery"
    assert diagnostics["cloud"]["serviceModules"][0]["name"] == "1"
    assert diagnostics["cloud"]["properties"]["ipAddress"] == bgc._REDACTED
    assert diagnostics["probe"]["status"] == "disabled_passive_mode"
    assert diagnostics["listener"]["mode"] == "passive_hub_listener"
    assert diagnostics["listener"]["registered"] is True
    assert diagnostics["listener"]["deviceUpdateCount"] == 0
    for secret in (
        "secret-device-id",
        "AA:BB:CC:DD:EE:FF",
        "Back Garden",
        "secret-mesh-key",
        "Private WiFi",
        "secret-hub-id",
        "secret-password",
        "testkey",
        "secret-user-id",
    ):
        assert secret not in encoded


def test_cloud_telemetry_initializes_and_refreshes() -> None:
    """Battery, mesh RSSI, and charge state follow refreshed cloud properties."""
    device = {
        "id": "devA",
        "mac": "AA:BB",
        "name": "Spot 1",
        "product": {
            "serviceModules": [
                {
                    "identity": "sensor.charging",
                    "properties": [{"identity": "chargeState"}],
                },
                {
                    "identity": "control.light.effect.mode",
                    "properties": [
                        {
                            "identity": "EffectMode",
                            "allowedValues": [
                                {"description": "party", "value": 5},
                                {"description": "bonfire", "value": 10},
                            ],
                        }
                    ],
                }
            ]
        },
        "properties": {
            "Battery_remaining": "65",
            "meshNetRssi": "-40",
            "chargeState": "1",
            "energySavingEnable": "1",
            "energySavingFactor": "0.8",
            "lightDuration": "30",
            "SecurityAlarm": "2",
            "alarmStatus": "0",
            "EffectMode": "5",
        },
    }
    hub = {
        "id": "hub1",
        "password": "pw",
        "aesKey": ["testkey"],
        "properties": {"ipAddress": "10.0.0.5"},
    }
    client = bgc.BleGatewayDeviceClient(device, hub, "user1")
    seen = []
    client.on_status_update = seen.append

    assert client.status.battery == 65
    assert client.status.mesh_rssi == -40
    assert client.status.charging is True
    assert client.status.energy_saving_enabled is True
    assert client.status.energy_saving_factor == 80
    assert client.status.light_duration == 30
    assert client.status.security_alarm is True
    assert client.status.alarm_status is False
    assert client.status.effect_mode == 5
    assert client.info.effects == {"Party": 5, "Bonfire": 10}

    client.update_cloud_properties(
        {
            **device,
            "properties": {
                "Battery_remaining": "54",
                "meshNetRssi": "-67",
                "chargeState": "0",
                "OnOff": "1",
                "Dimming": "20",
                "CCT": "3000",
                "RGBW": str(0x10203040),
            },
        }
    )

    assert client.status.battery == 54
    assert client.status.mesh_rssi == -67
    assert client.status.charging is False
    assert client.status.on is True
    assert client.status.dimming == 51
    assert client.status.cct == 3000
    assert client.status.rgbw == (0x10, 0x20, 0x30, 0x40)
    assert client.status.energy_saving_enabled is True
    assert client.status.energy_saving_factor == 80
    assert seen and seen[-1] is client.status


@pytest.mark.parametrize("cached_charge", ["0", "1"])
def test_unadvertised_cloud_charging_stays_unknown(cached_charge) -> None:
    """A generic cloud field and rising battery are not charging evidence."""
    client = _make_diagnostic_client()
    assert client.status.charging is None
    assert client.supports_telemetry_attribute("chargeState")

    client.update_cloud_properties({
        "properties": {"chargeState": cached_charge, "Battery_remaining": 90},
        "product": {"serviceModules": []},
    })
    client._handle_hub_update({"Battery_remaining": 91, "RGBW": 0})

    assert client.status.battery == 91
    assert client.status.charging is None
    assert client.charging_diagnostics == {
        "state_source": "not_reported",
        "last_reported": None,
        "cloud_attribute_advertised": False,
    }


@pytest.mark.parametrize("transport", ["hub", "cloud"])
def test_live_charging_wins_over_cloud_snapshots(transport) -> None:
    """Actual charging transitions work even without a declared capability."""
    client = _make_diagnostic_client()
    client._handle_hub_connection(False)
    notify = (
        client._handle_hub_update if transport == "hub"
        else client.handle_cloud_notification
    )
    notify({"chargeState": "1"})
    assert client.status.charging is True
    report_time = client.charging_diagnostics["last_reported"]
    assert report_time is not None
    assert client.charging_diagnostics["state_source"] == "device_report"
    assert client.status.online is (transport == "hub")

    # Even an advertised cloud snapshot must not overwrite newer live evidence.
    client.update_cloud_properties({
        "properties": {"chargeState": "0"},
        "product": {"serviceModules": [{"properties": [{"identity": "chargeState"}]}]},
    })
    notify({"Battery_remaining": 100, "OnOff": 0})
    notify({"chargeState": 2})
    assert client.status.charging is True
    assert client.charging_diagnostics["last_reported"] == report_time

    notify({"chargeState": "0"})
    assert client.status.charging is False
    assert client.charging_diagnostics["state_source"] == "device_report"
    client.update_cloud_properties({"properties": {"chargeState": "1"}})
    assert client.status.charging is False


def test_removed_cloud_charging_capability_clears_unverified_state() -> None:
    """A former cloud-only value must not remain when support disappears."""
    client = _make_client()
    client.update_cloud_properties({
        "properties": {"chargeState": "1"},
        "product": {"serviceModules": [{"properties": [{"identity": "chargeState"}]}]},
    })
    assert client.status.charging is True
    assert client.charging_diagnostics["state_source"] == "cloud_snapshot"
    client.update_cloud_properties({"properties": {"chargeState": "0"}})
    assert client.status.charging is None
    assert client.charging_diagnostics["state_source"] == "not_reported"


async def test_passive_update_refreshes_light_and_telemetry(monkeypatch) -> None:
    """An unsolicited hub frame updates state without querying the BLE light."""
    server = FakeServer([
        {
            "frames": [
                _login_frame(),
                _attr_frame(
                    {
                        "OnOff": 1,
                        "Dimming": 40,
                        "CCT": 3200,
                        "RGBW": 0x11223344,
                        "Battery_remaining": 34,
                    },
                    dev_id="secret-device-id",
                ),
            ]
        }
    ])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    client = _make_diagnostic_client()
    seen: list[bool] = []
    client.on_status_update = lambda status: seen.append(status.on)
    await bgc.start_all_hub_sessions()
    await asyncio.sleep(0)

    assert client.status.on is True
    assert client.status.dimming == 102
    assert client.status.cct == 3200
    assert client.status.rgbw == (0x11, 0x22, 0x33, 0x44)
    assert client.status.battery == 34
    assert client.status.telemetry_connected is True
    assert client.status.last_telemetry_update is not None
    assert seen == [False, True]

    messages = [
        json.loads(bgc._aes_decrypt(data[8:], KEY))
        for data in server.writers[0].writes
    ]
    assert [message["method"] for message in messages] == ["loginReq"]


async def test_passive_updates_are_routed_to_the_correct_device(monkeypatch) -> None:
    """One shared hub reader must never apply one light's update to another."""
    server = FakeServer([
        {
            "frames": [
                _login_frame(),
                _attr_frame({"OnOff": 1}, dev_id="devA"),
                _attr_frame(
                    {"OnOff": 0, "Battery_remaining": 72}, dev_id="devB"
                ),
            ]
        }
    ])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    client_a = _make_client("devA", "Spot A")
    client_b = _make_client("devB", "Spot B")
    await bgc.start_all_hub_sessions()
    await asyncio.sleep(0)

    assert client_a.status.on is True
    assert client_a.status.battery is None
    assert client_b.status.on is False
    assert client_b.status.battery == 72


async def test_passive_autonomous_off_is_forwarded(monkeypatch) -> None:
    """A fixture's timer-driven off update reaches Home Assistant callbacks."""
    server = FakeServer([
        {
            "frames": [
                _login_frame(),
                _attr_frame({"OnOff": 1}, dev_id="devA"),
                _attr_frame({"OnOff": 0}, dev_id="devA"),
            ]
        }
    ])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    client = _make_client()
    states: list[bool] = []
    client.on_status_update = lambda status: states.append(status.on)
    await bgc.start_all_hub_sessions()
    await asyncio.sleep(0)

    assert states == [False, True, False]
    assert client.status.on is False


async def test_optimistic_status_updates_after_command(monkeypatch):
    """async_set_brightness still updates cached status and fires the callback."""
    server = FakeServer([{"frames": [_login_frame(), _ack_frame()]}])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    client = _make_client()
    seen = []
    client.on_status_update = seen.append

    await client.async_set_brightness(255)

    assert client.status.on is True
    assert client.status.dimming == 255
    assert seen and seen[-1] is client.status


async def test_effect_command_updates_cached_mode(monkeypatch):
    """BLE effect commands use EffectMode and update coordinator memory."""
    server = FakeServer([{"frames": [_login_frame(), _ack_frame()]}])
    monkeypatch.setattr(bgc.asyncio, "open_connection", server.open_connection)

    client = _make_client()
    await client.async_set_effect(10)

    request = next(
        json.loads(bgc._aes_decrypt(data[8:], KEY))
        for data in server.writers[0].writes
        if json.loads(bgc._aes_decrypt(data[8:], KEY)).get("method")
        == "setDevAttrReq"
    )
    assert request["payload"]["attr"] == {"OnOff": 1, "EffectMode": 10}
    assert client.status.effect_mode == 10
