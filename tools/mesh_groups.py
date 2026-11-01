"""Inspect existing AiDot mesh groups without sending device commands.

Run inside the Home Assistant Core container, where python-aidot is installed:
    python tools/mesh_groups.py --config-dir /config

Account credentials are read from the selected existing config entry and used
only with AiDot's API. Output omits credentials, cloud IDs and device names.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class GroupInventoryError(ValueError):
    """An API response or existing group cannot be used as a local mesh target."""


def anonymous_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


def dynamic_group_address(value: Any) -> int:
    """Accept decimal API values or explicit hexadecimal dynamic addresses.

    Exclude unicast, virtual, reserved and fixed all-node addresses. An existing
    dynamic group is the investigation target; a whole-network broadcast is not.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise GroupInventoryError("Mesh group address must be an integer")
    try:
        address = (
            int(value, 16 if value.lower().startswith("0x") else 10)
            if isinstance(value, str)
            else value
        )
    except ValueError as error:
        raise GroupInventoryError("Invalid mesh group address") from error
    if not 0xC000 <= address <= 0xFEFF:
        raise GroupInventoryError("Address is not a dynamic mesh group address")
    return address


def inspect_group(group: dict, devices: list[dict]) -> dict[str, Any]:
    """Resolve every member to one known hub, never silently dropping members."""
    if group.get("bleMeshGroupType") != "normal":
        raise GroupInventoryError("Only normal mesh groups have been inspected")
    address = dynamic_group_address(group.get("bleMeshGroupAddress"))
    group_id = group.get("id")
    if not isinstance(group_id, str) or not group_id:
        raise GroupInventoryError("Group has no cloud identifier")
    members = group.get("deviceIds")
    if not isinstance(members, list) or not members or any(
        not isinstance(member, str) or not member for member in members
    ):
        raise GroupInventoryError("Group must contain an explicit nonempty member list")
    if len(set(members)) != len(members):
        raise GroupInventoryError("Group contains duplicate members")
    by_id = {
        device["id"]: device
        for device in devices
        if isinstance(device.get("id"), str)
    }
    hubs = set()
    for member in members:
        device = by_id.get(member)
        if device is None:
            raise GroupInventoryError("Group contains an unknown member")
        if device.get("type") != "light" or not device.get("bleMeshDeviceKey"):
            raise GroupInventoryError("Group contains a member outside the BLE light path")
        hub_id = device.get("directGateway")
        hub = by_id.get(hub_id) if isinstance(hub_id, str) else None
        if hub is None or hub.get("type") != "BleMesh_Hub":
            raise GroupInventoryError("Group member has no known mesh hub")
        hubs.add(hub_id)
    if len(hubs) != 1:
        raise GroupInventoryError("Group spans multiple mesh hubs")
    return {
        "anonymousGroupId": anonymous_id(group_id),
        "anonymousHubId": anonymous_id(next(iter(hubs))),
        "memberCount": len(members),
        "meshAddress": address,
        "meshAddressHex": f"0x{address:04X}",
        "localCommandVerified": False,
    }


def inventory_report(groups: list[dict], devices: list[dict]) -> dict:
    """Report eligible destinations separately from groups requiring more work."""
    report: dict[str, Any] = {"groups": [], "excluded": [], "commandsSent": 0}
    for group in groups:
        try:
            report["groups"].append(inspect_group(group, devices))
        except GroupInventoryError as error:
            report["excluded"].append({"reason": str(error)})
    return report


def load_login(config_dir: Path, entry_id: str | None) -> dict:
    entries = json.loads(
        (config_dir / ".storage/core.config_entries").read_text()
    )["data"]["entries"]
    matches = [
        entry
        for entry in entries
        if entry.get("domain") == "aidot"
        and (entry_id is None or entry.get("entry_id") == entry_id)
    ]
    if len(matches) != 1:
        raise GroupInventoryError("Select one existing AiDot entry with --entry-id")
    data = matches[0]["data"]
    login = data.get("login_info", data.get("loginInfo", data))
    if not isinstance(login, dict) or not login.get("accessToken"):
        raise GroupInventoryError("Existing AiDot entry has no saved access token")
    # Keep the saved region from becoming an arbitrary credential destination.
    if login.get("region") not in {"us", "eu", "cn", "jp", "US", "EU", "CN", "JP"}:
        raise GroupInventoryError("Existing AiDot entry has an unsupported region")
    return login


def discover(login: dict) -> dict:
    from aidot.login_const import APP_ID, API_URL_TEMPLATE

    base = API_URL_TEMPLATE.format(region=login["region"].lower())
    headers = {"Appid": APP_ID, "Terminal": "app", "Token": login["accessToken"]}

    def get_list(path: str) -> list[dict]:
        with urlopen(Request(base + path, headers=headers), timeout=30) as response:
            data = json.load(response)
        if not isinstance(data, list) or any(
            not isinstance(item, dict) for item in data
        ):
            raise GroupInventoryError("AiDot returned an unexpected list response")
        return data

    groups: list[dict] = []
    devices: list[dict] = []
    for house in get_list("/houses"):
        if house.get("isOwner") is not True:
            continue
        house_id = house.get("id")
        if not isinstance(house_id, str) or not house_id:
            raise GroupInventoryError("Owned house has no identifier")
        query = "?houseId=" + quote(house_id, safe="")
        devices.extend(get_list("/devices" + query))
        groups.extend(get_list("/groups" + query))
    return inventory_report(groups, devices)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--entry-id", help="Required if multiple AiDot entries exist")
    args = parser.parse_args()
    try:
        report = discover(load_login(args.config_dir, args.entry_id))
    except GroupInventoryError as error:
        parser.exit(1, f"AiDot group discovery failed: {error}.\n")
    except HTTPError as error:
        # Do not print HTTP bodies, URLs, tokens or saved account data.
        parser.exit(1, f"AiDot group discovery failed (HTTP {error.code}).\n")
    except (OSError, URLError, KeyError, TypeError, ValueError, ImportError) as error:
        parser.exit(1, f"AiDot group discovery failed ({type(error).__name__}).\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
