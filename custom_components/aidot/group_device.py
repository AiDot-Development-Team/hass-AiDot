"""Light adapter for one existing AiDot mesh group."""

from __future__ import annotations

import asyncio
import ctypes
from collections.abc import Callable
from dataclasses import dataclass, field

from .group_cloud import GroupCloudClient, GroupUnavailable
from .group_inventory import inspect_group
from .group_protocol import validate_attributes

# Validated with a watched group sequence and actual member notifications.
VERIFIED_GROUP_ATTRIBUTES = frozenset({"OnOff", "Dimming", "RGBW", "CCT", "EffectMode"})


@dataclass
class GroupInfo:
    dev_id: str
    name: str
    enable_rgbw: bool = False
    enable_cct: bool = False
    enable_dimming: bool = False
    effects: dict[str, int] = field(default_factory=dict)
    cct_min: int = 2700
    cct_max: int = 6500
    model_id: str = "AiDot.MeshGroup"
    mac: str = ""
    hw_version: str | None = None


@dataclass
class GroupStatus:
    online: bool = False
    on: bool | None = None
    dimming: int | None = None
    cct: int | None = None
    rgbw: tuple[int, int, int, int] | None = None
    effect_mode: int | None = None
    active_color_mode: str | None = None


class GroupDeviceClient:
    """Use the existing light platform's batch attribute interface."""

    def __init__(
        self,
        home_id: str,
        group: dict,
        devices: list[dict],
        members: list,
        cloud: GroupCloudClient,
    ) -> None:
        self.cloud = cloud
        self.status = GroupStatus()
        self.on_status_update: Callable | None = None
        self.info = GroupInfo(
            dev_id=f"group:{home_id}:{group['id']}",
            name=group.get("name") or "AiDot group",
        )
        self._lock = asyncio.Lock()
        self.valid = True
        self.last_acknowledgement_ms: float | None = None
        self.update(home_id, group, devices, members)

    def update(
        self, home_id: str, group: dict, devices: list[dict], members: list
    ) -> None:
        report = inspect_group(group, devices)
        if {member.info.dev_id for member in members} != set(group["deviceIds"]):
            raise ValueError("Group is missing a current member client")
        self.home_id = home_id
        self.group = group.copy()
        self.group["deviceIds"] = list(group["deviceIds"])
        self.devices = devices
        self.members = members
        self.hub_id = next(
            d["directGateway"] for d in devices if d.get("id") == group["deviceIds"][0]
        )
        self.report = report
        self.valid = True
        self.info.name = group.get("name") or "AiDot group"
        common: set[str] | None = None
        for device in devices:
            if device.get("id") not in group["deviceIds"]:
                continue
            attrs = set(device.get("properties", {}))
            for module in device.get("product", {}).get("serviceModules", []):
                attrs.update(
                    p.get("identity")
                    for p in module.get("properties", [])
                    if isinstance(p, dict)
                )
            common = attrs if common is None else common & attrs
        self.attributes = (common or set()) & VERIFIED_GROUP_ATTRIBUTES | {"OnOff"}
        self.info.enable_dimming = "Dimming" in self.attributes
        self.info.enable_rgbw = "RGBW" in self.attributes and all(
            m.info.enable_rgbw for m in members
        )
        self.info.enable_cct = "CCT" in self.attributes and all(
            m.info.enable_cct for m in members
        )
        self.info.cct_min = max(m.info.cct_min for m in members)
        self.info.cct_max = min(m.info.cct_max for m in members)
        if self.info.cct_min > self.info.cct_max:
            self.info.enable_cct = False
            self.attributes.discard("CCT")
        effects = members[0].info.effects.copy()
        self.info.effects = {
            name: code
            for name, code in effects.items()
            if "EffectMode" in self.attributes
            and all(m.info.effects.get(name) == code for m in members)
        }
        revision = (
            home_id,
            group["id"],
            report["meshAddress"],
            tuple(sorted(group["deviceIds"])),
            tuple(sorted(self.attributes)),
            self.info.cct_min,
            self.info.cct_max,
            tuple(sorted(self.info.effects.items())),
        )
        if hasattr(self, "_revision") and self._revision != revision:
            self.status = GroupStatus()
        self._revision = revision
        self.set_connected(self.cloud.connected)

    def set_connected(self, connected: bool) -> None:
        self.status.online = self.valid and connected
        if self.on_status_update:
            self.on_status_update(self.status)

    def invalidate(self) -> None:
        self.valid = False
        self.set_connected(False)

    def apply_accepted_attributes(self, attributes: dict) -> None:
        """Record assumed group state without manufacturing member reports."""
        if "OnOff" in attributes:
            self.status.on = bool(attributes["OnOff"])
        if "Dimming" in attributes:
            self.status.dimming = int(attributes["Dimming"] * 255 / 100)
        if "RGBW" in attributes:
            rgbw = ctypes.c_uint32(attributes["RGBW"]).value
            self.status.rgbw = tuple((rgbw >> shift) & 255 for shift in (24, 16, 8, 0))
            self.status.active_color_mode = "rgbw"
        if "CCT" in attributes:
            self.status.cct = attributes["CCT"]
            self.status.active_color_mode = "color_temp"
        if "EffectMode" in attributes:
            self.status.effect_mode = attributes["EffectMode"] or None
        if self.on_status_update:
            self.on_status_update(self.status)

    async def send_dev_attr(self, attributes: dict[str, int]) -> None:
        attributes = validate_attributes(attributes)
        revision = self._revision
        if set(attributes) - self.attributes:
            raise ValueError("An attribute is not supported by every group member")
        if (
            "CCT" in attributes
            and not self.info.cct_min <= attributes["CCT"] <= self.info.cct_max
        ):
            raise ValueError("Color temperature is outside the common group range")
        if "EffectMode" in attributes and attributes["EffectMode"] not in {
            0,
            *self.info.effects.values(),
        }:
            raise ValueError("Effect is not supported by every group member")
        if (
            "RGBW" in attributes or "CCT" in attributes
        ) and "EffectMode" in self.attributes:
            attributes.setdefault("EffectMode", 0)
        async with self._lock:
            if not self.valid or not self.cloud.connected:
                raise GroupUnavailable("AiDot group is unavailable")
            if revision != self._revision:
                raise GroupUnavailable("AiDot group membership changed before dispatch")
            group, devices, home_id = self.group, self.devices, self.home_id

            async def send() -> float:
                if not self.valid or revision != self._revision:
                    raise GroupUnavailable(
                        "AiDot group membership changed before dispatch"
                    )
                return await self.cloud.send(home_id, group, devices, attributes)

            try:
                member = self.members[0]
                self.last_acknowledgement_ms = await member.async_run_group_command(
                    send
                )
            except TimeoutError as error:
                raise GroupUnavailable(
                    "AiDot did not confirm the group command; delivery is uncertain"
                ) from error
            if revision == self._revision and self.valid:
                self.apply_accepted_attributes(attributes)

    async def async_turn_off(self) -> None:
        await self.send_dev_attr({"OnOff": 0})

    async def async_turn_on(self) -> None:
        await self.send_dev_attr({"OnOff": 1})
