"""Validate existing owned mesh groups without changing their membership."""

from __future__ import annotations

import hashlib
from typing import Any


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
    if (
        not isinstance(members, list)
        or not members
        or any(not isinstance(member, str) or not member for member in members)
    ):
        raise GroupInventoryError("Group must contain an explicit nonempty member list")
    if len(set(members)) != len(members):
        raise GroupInventoryError("Group contains duplicate members")
    by_id = {
        device["id"]: device for device in devices if isinstance(device.get("id"), str)
    }
    hubs = set()
    for member in members:
        device = by_id.get(member)
        if device is None:
            raise GroupInventoryError("Group contains an unknown member")
        if device.get("type") != "light" or not device.get("bleMeshDeviceKey"):
            raise GroupInventoryError(
                "Group contains a member outside the BLE light path"
            )
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
