"""Verify optional group setup and routing without a running HA instance."""

import asyncio
import importlib
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def manager_module(monkeypatch):
    class Entry:
        def __class_getitem__(cls, item):
            return cls

    class Coordinator:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, hass, logger, **kwargs):
            self.hass, self.data = hass, None
            self.config_entry = kwargs.get("config_entry")
            self.listeners = []

        async def async_config_entry_first_refresh(self):
            await self._async_setup()
            self.data = await self._async_update_data()

        def async_set_updated_data(self, data):
            self.data = data

        async def async_request_refresh(self):
            await self._async_update_data()

    modules = {
        "homeassistant": {},
        "homeassistant.config_entries": {"ConfigEntry": Entry},
        "homeassistant.core": {"HomeAssistant": object},
        "homeassistant.exceptions": {
            "ConfigEntryAuthFailed": type("ConfigEntryAuthFailed", (Exception,), {})
        },
        "homeassistant.helpers": {},
        "homeassistant.helpers.aiohttp_client": {
            "async_get_clientsession": lambda hass: None
        },
        "homeassistant.helpers.update_coordinator": {
            "DataUpdateCoordinator": Coordinator
        },
        "homeassistant.helpers.device_registry": {
            "async_get": lambda hass: SimpleNamespace(async_update_device=Mock()),
            "async_entries_for_config_entry": lambda registry, entry_id: [],
        },
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    package_name = "aidot_group_manager_test"
    package = ModuleType(package_name)
    package.__path__ = [str(Path(__file__).parents[1] / "custom_components/aidot")]
    monkeypatch.setitem(sys.modules, package_name, package)
    module = importlib.import_module(package_name + ".coordinator")
    yield module
    for name in list(sys.modules):
        if name.startswith(package_name + "."):
            del sys.modules[name]


def group_data():
    group = {
        "id": "front",
        "name": "Example garden",
        "bleMeshGroupType": "normal",
        "bleMeshGroupAddress": "49289",
        "deviceIds": ["one", "two"],
    }
    devices = [{"id": "hub", "type": "BleMesh_Hub", "properties": {}}]
    devices += [
        {
            "id": member,
            "type": "light",
            "directGateway": "hub",
            "bleMeshDeviceKey": "secret",
            "modelId": "LK.A000181",
            "properties": {"OnOff": 0},
        }
        for member in group["deviceIds"]
    ]
    return group, devices


async def build_manager(module, monkeypatch):
    group, devices = group_data()

    class Vendor:
        def __init__(self, **kwargs):
            self.login_info = dict(kwargs["token"])
            self.async_get_all_device = AsyncMock(return_value={"device_list": devices})
            self.async_cleanup = AsyncMock()
            self.async_post_login = AsyncMock()

        def set_token_fresh_cb(self, callback):
            self.callback = callback

    # Use the library's real constant, rather than assuming a payload spelling.
    from aidot.const import CONF_DEVICE_LIST

    monkeypatch.setattr(module, "AidotClient", Vendor)
    monkeypatch.setattr(module, "start_all_hub_sessions", AsyncMock())
    entry = SimpleNamespace(
        data={"id": "user", "accessToken": "token", "region": "us"},
        entry_id="entry",
        async_start_reauth=Mock(),
    )

    def update_entry(entry, **kwargs):
        entry.data = kwargs["data"]

    hass = SimpleNamespace(
        async_create_task=asyncio.create_task,
        config_entries=SimpleNamespace(
            async_update_entry=Mock(side_effect=update_entry)
        ),
    )
    manager = module.AidotDeviceManagerCoordinator(hass, entry)
    manager.client.async_get_all_device.return_value = {CONF_DEVICE_LIST: devices}
    manager.group_cloud.discover = AsyncMock(return_value=([("home", group)], 0))
    manager.group_cloud.start = Mock()
    await manager._async_update_data()
    return manager, group, devices


async def test_group_discovery_keeps_physical_sensor_coordinators_separate(
    manager_module, monkeypatch
):
    manager, _, _ = await build_manager(manager_module, monkeypatch)
    try:
        assert set(manager.device_coordinators) == {"one", "two"}
        assert set(manager.group_coordinators) == {"group:home:front"}
        group = manager.group_coordinators["group:home:front"].device_client
        assert group.status.on is None
        assert group.status.online is False
        manager.group_cloud._set_connected(True)
        assert group.status.online is True
        assert group.report["memberCount"] == 2
    finally:
        await manager.async_cleanup()


async def test_group_failure_does_not_stop_local_devices_and_invalidates_old_membership(
    manager_module, monkeypatch
):
    manager, _, _ = await build_manager(manager_module, monkeypatch)
    try:
        old_clients = {
            key: c.device_client for key, c in manager.device_coordinators.items()
        }
        manager.group_cloud.discover.side_effect = ConnectionError("cloud unavailable")
        await manager._async_update_data()
        assert {
            key: c.device_client for key, c in manager.device_coordinators.items()
        } == old_clients
        assert (
            manager.group_coordinators["group:home:front"].device_client.valid is False
        )
        assert manager.group_status["lastDiscoveryErrorType"] == "ConnectionError"
        assert manager._group_retry_task is not None
    finally:
        await manager.async_cleanup()


async def test_removed_and_readded_group_reuses_stable_coordinator(
    manager_module, monkeypatch
):
    manager, group, _ = await build_manager(manager_module, monkeypatch)
    try:
        old = manager.group_coordinators["group:home:front"]
        manager.group_cloud.discover.return_value = ([], 0)
        await manager._async_update_data()
        assert old.device_client.valid is False
        renamed = deepcopy(group)
        renamed["name"] = "Renamed"
        manager.group_cloud.discover.return_value = ([("home", renamed)], 0)
        await manager._async_update_data()
        assert manager.group_coordinators["group:home:front"] is old
        assert old.device_client.valid is True
        assert old.device_client.info.name == "Renamed"
    finally:
        await manager.async_cleanup()


async def test_route_only_actual_member_notifications_from_known_hub(
    manager_module, monkeypatch
):
    manager, _, _ = await build_manager(manager_module, monkeypatch)
    try:
        client = manager.device_coordinators["one"].device_client
        message = {
            "payload": {"devId": "one", "attr": {"OnOff": 1, "Battery_remaining": 64}}
        }
        manager._handle_group_cloud_message(
            "iot/v1/cb/other-hub/device/setDevAttrNotif", message
        )
        manager._handle_group_cloud_message(
            "iot/v1/cb/hub/device/setDevAttrResp", message
        )
        assert client.status.on is False and client.status.last_telemetry_update is None
        manager._handle_group_cloud_message(
            "iot/v1/cb/hub/device/setDevAttrNotif", message
        )
        assert client.status.on is True and client.status.battery == 64
        assert client.status.last_telemetry_update is not None
        assert client.status.online is False
        assert client.status.telemetry_connected is True
        client._handle_hub_connection(True)
        manager._handle_group_cloud_message("iot/v1/cb/hub/device/setDevAttrNotif", message)
        assert client.status.online is True
        assert manager.device_coordinators["two"].device_client.status.on is False
    finally:
        await manager.async_cleanup()


async def test_phone_group_ack_updates_assumed_group_but_not_every_member(
    manager_module, monkeypatch
):
    manager, _, _ = await build_manager(manager_module, monkeypatch)
    try:
        message = {
            "service": "group",
            "method": "excGroupResp",
            "seq": "phone-sequence",
            "ack": {"code": 200},
            "payload": {
                "method": "set_prop",
                "groupId": "front",
                "homeId": "home",
                "bleMeshGroupAddress": "49289",
                "params": {"OnOff": 1},
            },
        }
        manager._handle_group_cloud_message("iot/v1/c/user/group/excGroupResp", message)
        assert (
            manager.group_coordinators["group:home:front"].device_client.status.on
            is True
        )
        assert all(
            c.device_client.status.on is False
            for c in manager.device_coordinators.values()
        )
        message["payload"]["params"]["OnOff"] = 0
        message["payload"]["bleMeshGroupAddress"] = "65535"
        manager._handle_group_cloud_message("iot/v1/c/user/group/excGroupResp", message)
        assert (
            manager.group_coordinators["group:home:front"].device_client.status.on
            is True
        )
    finally:
        await manager.async_cleanup()


async def test_group_auth_recovery_saves_new_token_once(manager_module, monkeypatch):
    manager, _, _ = await build_manager(manager_module, monkeypatch)
    try:

        async def login():
            manager.client.login_info["accessToken"] = "new-token"

        manager.client.async_post_login.side_effect = login
        await manager._async_recover_group_auth("token")
        assert manager.config_entry.data["accessToken"] == "new-token"
        assert manager.cloud_status["reauthenticationCount"] == 1
        await manager._async_recover_group_auth("token")
        manager.client.async_post_login.assert_awaited_once()
    finally:
        await manager.async_cleanup()
