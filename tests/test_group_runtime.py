"""Exercise group transport correlation, reconnects, capabilities and isolation."""

import asyncio
import importlib
import ssl
import sys
import threading
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def runtime(monkeypatch):
    name = "aidot_group_runtime_test"
    package = ModuleType(name)
    package.__path__ = [str(Path(__file__).parents[1] / "custom_components/aidot")]
    monkeypatch.setitem(sys.modules, name, package)
    modules = {
        part: importlib.import_module(name + "." + part)
        for part in ("group_inventory", "group_protocol", "group_cloud", "group_device")
    }
    yield SimpleNamespace(
        **{key.removeprefix("group_"): value for key, value in modules.items()}
    )
    for key in list(sys.modules):
        if key.startswith(name + "."):
            del sys.modules[key]


def inventory():
    group = {
        "id": "group",
        "name": "Test group",
        "bleMeshGroupType": "normal",
        "bleMeshGroupAddress": "49289",
        "deviceIds": ["one", "two"],
    }
    devices = [{"id": "hub", "type": "BleMesh_Hub"}]
    devices += [
        {
            "id": member,
            "type": "light",
            "directGateway": "hub",
            "bleMeshDeviceKey": "secret",
            "properties": {
                "OnOff": 0,
                "RGBW": 0,
                "CCT": 2700,
                "Dimming": 100,
                "EffectMode": 0,
            },
        }
        for member in group["deviceIds"]
    ]
    return group, devices


def command(protocol, attributes):
    group, devices = inventory()
    return protocol.build_group_request(
        group=group,
        devices=devices,
        home_id="home",
        user_id="user",
        client_id="session-user",
        attributes=attributes,
        sequence="sequence",
        unix_time=100,
    )


@pytest.mark.parametrize(
    "attributes",
    [
        {"OnOff": 1, "Dimming": 50, "RGBW": -16777216, "EffectMode": 0},
        {"OnOff": 1, "CCT": 2700},
        {"OnOff": 1, "EffectMode": 5},
    ],
)
def test_compound_requests_and_complete_echo(runtime, attributes):
    _, request = command(runtime.protocol, attributes)
    assert request["payload"]["params"] == attributes
    message = {
        "method": "excGroupResp",
        "service": "group",
        "seq": "sequence",
        "ack": {"code": 200},
        "payload": deepcopy(request["payload"]),
    }
    assert (
        runtime.protocol.group_response_code(
            topic="iot/v1/c/user/group/excGroupResp",
            message=message,
            request=request,
            user_id="user",
        )
        == 200
    )
    message["payload"]["params"] = {"OnOff": 1}
    with pytest.raises(runtime.protocol.GroupProtocolError):
        runtime.protocol.group_response_code(
            topic="iot/v1/c/user/group/excGroupResp",
            message=message,
            request=request,
            user_id="user",
        )


@pytest.mark.parametrize(
    "attrs",
    [
        {},
        {"pair": 1},
        {"Dimming": 101},
        {"OnOff": True},
        {"RGBW": 2**32},
        {"EffectMode": -1},
        {"CCT": 0},
        {"RGBW": 0, "CCT": 2700},
    ],
)
def test_refuse_unproven_or_invalid_writes(runtime, attrs):
    with pytest.raises(runtime.protocol.GroupProtocolError):
        command(runtime.protocol, attrs)


@pytest.mark.parametrize(
    "config",
    [
        {
            "host": "attacker.example:443",
            "clientId": "session-user",
            "password": "secret",
        },
        {
            "host": "good.arnoo.com/other",
            "clientId": "session-user",
            "password": "secret",
        },
        {
            "host": "evil@global-us-mqtt.arnoo.com:8443",
            "clientId": "session-user",
            "password": "secret",
        },
        {
            "host": "global-us-mqtt.arnoo.com:8443",
            "clientId": "other-user2",
            "password": "secret",
        },
    ],
)
def test_refuse_wrong_mqtt_account_or_destination(runtime, config):
    with pytest.raises(runtime.protocol.GroupProtocolError):
        runtime.cloud.mqtt_destination(config, "user")


class FakeMqtt:
    def __init__(self, identity):
        self.identity = identity
        self.published = []
        self.mode = "ok"
        self.stopped = False
        self.disconnected = False
        self.codes = [0, 0]

    def username_pw_set(self, *args):
        self.auth = args

    def tls_set_context(self, context):
        self.tls = context

    def ws_set_options(self, **kwargs):
        self.ws = kwargs

    def connect(self, *args):
        self.destination = args

    def loop_start(self):
        # Real Paho invokes these from a separate network thread.
        thread = threading.Thread(
            target=lambda: self.on_connect(self, None, None, 0, None)
        )
        thread.start()
        thread.join()

    def subscribe(self, topics):
        self.topics = topics
        self.on_subscribe(self, None, 1, self.codes, None)

    def publish(self, topic, payload, **kwargs):
        import json

        request = json.loads(payload)
        self.published.append((topic, request, kwargs))
        message = {
            "method": "excGroupResp",
            "service": "group",
            "seq": request["seq"],
            "ack": {"code": 200},
            "payload": deepcopy(request["payload"]),
        }
        if self.mode == "wrong-target":
            message["payload"]["groupId"] = "other-group"
        elif self.mode == "rejected":
            message["ack"]["code"] = 403
        elif self.mode == "wrong-sequence":
            message["seq"] = "unrelated"
        self.last_reply = message
        if self.mode != "timeout":
            self.on_message(
                self,
                None,
                SimpleNamespace(
                    topic="iot/v1/c/user/group/excGroupResp",
                    payload=json.dumps(message).encode(),
                ),
            )
        return SimpleNamespace(rc=0)

    def disconnect(self):
        self.disconnected = True

    def loop_stop(self):
        self.stopped = True


async def connected_client(runtime):
    login = {"id": "user", "accessToken": "secret", "region": "us"}
    created, updates, messages = [], [], []

    def factory(identity):
        client = FakeMqtt(identity)
        created.append(client)
        return client

    cloud = runtime.cloud.GroupCloudClient(
        None,
        lambda: login,
        AsyncMock(),
        on_message=lambda t, m: messages.append((t, m)),
        on_connection=lambda state: updates.append((state, threading.get_ident())),
        mqtt_factory=factory,
    )
    cloud._get = AsyncMock(
        return_value={
            "host": "global-us-mqtt.arnoo.com:8443",
            "password": "secret-password",
            "clientId": "session-user",
            "heartbeat": 45,
        }
    )
    cloud.set_hubs({"hub"})
    await cloud._connect()
    return cloud, created, updates, messages, login


async def test_tls_and_callbacks_are_on_the_event_loop(runtime):
    cloud, created, updates, _, _ = await connected_client(runtime)
    try:
        client = created[0]
        assert client.tls.check_hostname is True
        assert client.tls.verify_mode == ssl.CERT_REQUIRED
        assert client.ws == {"path": "/mqtt"}
        assert client.topics == [("iot/v1/c/user/#", 0), ("iot/v1/cb/hub/#", 0)]
        assert updates == [(True, threading.get_ident())]
    finally:
        await cloud.close()
    assert client.disconnected and client.stopped


async def test_send_once_with_matching_reply_and_no_retained_or_queued_write(runtime):
    cloud, created, _, _, _ = await connected_client(runtime)
    try:
        group, devices = inventory()
        assert await cloud.send("home", group, devices, {"OnOff": 1}) >= 0
        assert len(created[0].published) == 1
        assert created[0].published[0][2] == {"qos": 0, "retain": False}
        assert cloud._pending == {}
    finally:
        await cloud.close()


@pytest.mark.parametrize(
    "mode", ["wrong-target", "rejected", "timeout", "wrong-sequence"]
)
async def test_failure_never_retries_and_pending_is_clean(runtime, monkeypatch, mode):
    monkeypatch.setattr(runtime.cloud, "_TIMEOUT", 0.03)
    cloud, created, _, messages, _ = await connected_client(runtime)
    try:
        created[0].mode = mode
        group, devices = inventory()
        with pytest.raises((runtime.protocol.GroupProtocolError, TimeoutError)):
            await cloud.send("home", group, devices, {"OnOff": 1})
        assert len(created[0].published) == 1
        assert cloud._pending == {}
        # A late reply from our timed-out command must not masquerade as a
        # new phone-app change and roll a later accepted state backwards.
        before = len(messages)
        if mode == "timeout":
            cloud._message(
                created[0], "iot/v1/c/user/group/excGroupResp", created[0].last_reply
            )
            assert len(messages) == before
    finally:
        await cloud.close()


async def test_close_wakes_inflight_command_without_replay(runtime):
    cloud, created, _, _, _ = await connected_client(runtime)
    created[0].mode = "timeout"
    group, devices = inventory()
    task = asyncio.create_task(cloud.send("home", group, devices, {"OnOff": 1}))
    await asyncio.sleep(0)
    await cloud.close()
    with pytest.raises(runtime.cloud.GroupUnavailable):
        await task
    assert len(created[0].published) == 1


async def test_reconnect_creates_fresh_client_and_sends_no_old_commands(
    runtime, monkeypatch
):
    monkeypatch.setattr(runtime.cloud, "_RECONNECT_MIN", 0.01)
    cloud, created, _, _, _ = await connected_client(runtime)
    await cloud._dispose()
    cloud.start()
    try:
        async with asyncio.timeout(2):
            while not cloud.connected:
                await asyncio.sleep(0.001)
        group, devices = inventory()
        await cloud.send("home", group, devices, {"OnOff": 1})
        old = created[-1]
        old.on_disconnect(old, None, None, 1, None)
        async with asyncio.timeout(2):
            while len(created) < 3 or not cloud.connected:
                await asyncio.sleep(0.001)
        assert old.disconnected and old.stopped
        assert created[-1] is not old
        assert created[-1].published == []
    finally:
        await cloud.close()


async def make_group(runtime, monkeypatch):
    monkeypatch.setattr(
        runtime.device,
        "VERIFIED_GROUP_ATTRIBUTES",
        frozenset({"OnOff", "RGBW", "CCT", "Dimming", "EffectMode"}),
    )
    group, devices = inventory()

    async def run(operation):
        return await operation()

    members = [
        SimpleNamespace(
            info=SimpleNamespace(
                dev_id=member,
                enable_rgbw=True,
                enable_cct=True,
                cct_min=2700,
                cct_max=6500,
                effects={"Normal": 0, "Cycle": 5},
            ),
            async_run_group_command=run,
        )
        for member in group["deviceIds"]
    ]
    cloud = SimpleNamespace(connected=True, send=AsyncMock(return_value=123.4))
    return runtime.device.GroupDeviceClient(
        "home", group, devices, members, cloud
    ), cloud


async def test_compound_group_color_exits_effect_once_without_changing_members(
    runtime, monkeypatch
):
    client, cloud = await make_group(runtime, monkeypatch)
    members_before = deepcopy(client.devices)
    await client.send_dev_attr({"OnOff": 1, "Dimming": 50, "RGBW": 16711680})
    cloud.send.assert_awaited_once_with(
        "home",
        client.group,
        client.devices,
        {"OnOff": 1, "Dimming": 50, "RGBW": 16711680, "EffectMode": 0},
    )
    assert client.status.on is True
    assert client.status.rgbw == (0, 255, 0, 0)
    assert client.status.dimming == 127
    assert client.devices == members_before


async def test_failed_group_write_does_not_claim_new_state(runtime, monkeypatch):
    client, cloud = await make_group(runtime, monkeypatch)
    before = deepcopy(client.status)
    cloud.send.side_effect = ConnectionError("offline")
    with pytest.raises(ConnectionError):
        await client.send_dev_attr({"OnOff": 1, "RGBW": 255})
    assert client.status == before


async def test_invalidate_prevents_stale_membership_write(runtime, monkeypatch):
    client, cloud = await make_group(runtime, monkeypatch)
    client.invalidate()
    with pytest.raises(runtime.cloud.GroupUnavailable):
        await client.async_turn_on()
    cloud.send.assert_not_awaited()


async def test_capabilities_are_member_intersection(runtime, monkeypatch):
    client, cloud = await make_group(runtime, monkeypatch)
    client.members[-1].info.effects = {"Normal": 0}
    client.members[-1].info.cct_min = 3000
    client.devices[-1]["properties"].pop("RGBW")
    client.update(client.home_id, client.group, client.devices, client.members)
    assert client.info.effects == {"Normal": 0}
    assert client.info.cct_min == 3000
    assert client.info.enable_rgbw is False
    with pytest.raises(ValueError):
        await client.send_dev_attr({"RGBW": 255})
    cloud.send.assert_not_awaited()


async def test_group_barrier_does_not_leak_local_pipeline_slots_on_cancellation():
    from ble_gateway_client import _MAX_IN_FLIGHT, _HubSession

    session = _HubSession("hub", "127.0.0.1", bytes(16), "user", "password")
    for _ in range(2):
        await session._command_slots.acquire()
    operation = AsyncMock()
    task = asyncio.create_task(session.run_group_command(operation))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    operation.assert_not_awaited()
    assert session._command_slots._value == _MAX_IN_FLIGHT - 2
    session._command_slots.release()
    session._command_slots.release()
    assert session._command_slots._value == _MAX_IN_FLIGHT


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            from aiohttp import ClientResponseError

            raise ClientResponseError(None, (), status=self.status)

    async def json(self):
        return self.data


async def test_http_401_recovers_once_using_current_token(runtime):
    login = {"id": "user", "accessToken": "old", "region": "us"}
    calls = []

    class Http:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response([], 401 if len(calls) == 1 else 200)

    async def recover(token):
        assert token == "old"
        login["accessToken"] = "new"

    cloud = runtime.cloud.GroupCloudClient(
        Http(),
        lambda: login,
        recover,
        on_message=lambda t, m: None,
        on_connection=lambda c: None,
    )
    assert await cloud._get("/houses") == []
    assert [call[1]["headers"]["token"] for call in calls] == ["old", "new"]
    assert all(call[1]["allow_redirects"] is False for call in calls)
    await cloud.close()


async def test_discovery_skips_shared_homes_and_ineligible_groups(runtime):
    group, devices = inventory()
    invalid = deepcopy(group)
    invalid["id"] = "bad"
    invalid["deviceIds"].append("unknown")
    calls = []

    class Http:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith("/houses"):
                return Response(
                    [
                        {"id": "shared", "isOwner": False},
                        {"id": "home", "isOwner": True},
                    ]
                )
            assert kwargs["params"] == {"houseId": "home"}
            return Response([group, invalid])

    login = {"id": "user", "accessToken": "secret", "region": "us"}
    cloud = runtime.cloud.GroupCloudClient(
        Http(),
        lambda: login,
        AsyncMock(),
        on_message=lambda t, m: None,
        on_connection=lambda c: None,
    )
    targets, excluded = await cloud.discover(devices)
    assert targets == [("home", group)] and excluded == 1
    assert len(calls) == 2
    await cloud.close()


async def test_diagnostics_do_not_contain_credentials_or_cloud_identifiers(runtime):
    cloud, _, _, _, _ = await connected_client(runtime)
    try:
        group, devices = inventory()
        await cloud.send("home", group, devices, {"OnOff": 1})
        text = str(cloud.diagnostics)
        for secret in ("secret", "session-user", "Test group", "global-us", "iot/v1"):
            assert secret not in text
    finally:
        await cloud.close()
