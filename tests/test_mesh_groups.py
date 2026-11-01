"""Group discovery must not silently widen or narrow a native mesh target."""

from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("mesh_groups", Path(__file__).parents[1] / "tools/mesh_groups.py")
groups = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(groups)


def sample():
    group = {"id": "group-secret-id", "name": "Private group name", "bleMeshGroupType": "normal", "bleMeshGroupAddress": "49153", "deviceIds": ["light-a", "light-b"]}
    devices = [{"id": "hub-secret-id", "type": "BleMesh_Hub", "password": "hub-secret-password", "aesKey": ["secret-key"]}]
    devices += [{"id": member, "type": "light", "directGateway": "hub-secret-id", "bleMeshDeviceKey": "secret-device-key"} for member in group["deviceIds"]]
    return group, devices


@pytest.mark.parametrize("value", [0xC000, "49152", "0xC000", 0xFEFF])
def test_accept_dynamic_addresses(value):
    assert 0xC000 <= groups.dynamic_group_address(value) <= 0xFEFF


@pytest.mark.parametrize("value", [None, True, 49153.9, "", "bad", 1, 0x8001, 0xFF00, 0xFFFF])
def test_refuse_non_dynamic_addresses(value):
    with pytest.raises(groups.GroupInventoryError):
        groups.dynamic_group_address(value)


def test_report_exact_membership_without_identifiers_or_credentials():
    group, devices = sample()
    report = groups.inventory_report([group], devices)
    assert report["groups"][0]["memberCount"] == 2
    assert report["groups"][0]["meshAddressHex"] == "0xC001"
    assert report["groups"][0]["localCommandVerified"] is False
    assert report["commandsSent"] == 0
    text = json.dumps(report)
    for secret in ["secret", "light-a", "light-b", "Private group name"]:
        assert secret not in text


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "empty", "wifi", "other-hub", "missing-hub", "other-group-type"])
def test_exclude_ineligible_group_without_changing_membership(mutation):
    group, devices = sample()
    if mutation == "unknown":
        group["deviceIds"].append("missing")
    elif mutation == "duplicate":
        group["deviceIds"].append("light-a")
    elif mutation == "empty":
        group["deviceIds"] = []
    elif mutation == "wifi":
        devices[-1].pop("bleMeshDeviceKey")
    elif mutation == "other-hub":
        devices.append({"id": "other-hub", "type": "BleMesh_Hub"})
        devices[-2]["directGateway"] = "other-hub"
    elif mutation == "missing-hub":
        devices.pop(0)
    else:
        group["bleMeshGroupType"] = "unknown"
    before = deepcopy((group, devices))
    report = groups.inventory_report([group], devices)
    assert report["groups"] == []
    assert len(report["excluded"]) == 1
    assert report["commandsSent"] == 0
    assert (group, devices) == before


def test_login_rejects_arbitrary_credential_destination(tmp_path):
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "core.config_entries").write_text(json.dumps({"data": {"entries": [{"domain": "aidot", "entry_id": "one", "data": {"region": "attacker.example/", "accessToken": "secret"}}]}}))
    with pytest.raises(groups.GroupInventoryError):
        groups.load_login(tmp_path, None)
