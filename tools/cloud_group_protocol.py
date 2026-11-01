"""Build and validate the observed AiDot cloud group on/off operation.

This research module performs no I/O. Callers must obtain the group and home
from an owned-home inventory, keep account/session identifiers private, and
use a separately authenticated MQTT session with TLS verification enabled.
Cloud acceptance does not establish physical delivery or BLE multicast.
"""

from __future__ import annotations

import re
from typing import Any

from tools.mesh_groups import inspect_group


class GroupProtocolError(ValueError):
    """A request or correlated reply cannot be safely interpreted."""


class GroupRejected(GroupProtocolError):
    """The cloud rejected a correlated group operation."""

    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"AiDot rejected the group operation (code {code})")


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(
        re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value)
    )


def build_on_off_request(
    *,
    group: dict,
    devices: list[dict],
    home_id: str,
    user_id: str,
    client_id: str,
    state: int,
    sequence: str,
    unix_time: int,
) -> tuple[str, dict]:
    """Target one existing eligible group; never fall back to all-node writes.

    The caller supplies a fresh sequence and its own WebPC client ID. This
    function neither retries nor generates per-member commands.
    """
    report = inspect_group(group, devices)
    if not all(_identifier(value) for value in (home_id, user_id, sequence)):
        raise GroupProtocolError("Invalid home, user or sequence identifier")
    if not _identifier(client_id) or not client_id.endswith("-" + user_id):
        raise GroupProtocolError("WebPC client ID does not belong to this user")
    if type(state) is not int or state not in (0, 1):
        raise GroupProtocolError("OnOff must be integer 0 or 1")
    if type(unix_time) is not int or unix_time < 0:
        raise GroupProtocolError("Timestamp must be a nonnegative integer")
    message = {
        "method": "excGroupReq",
        "service": "group",
        "seq": sequence,
        "srcAddr": "0." + user_id,
        "tst": unix_time,
        "clientId": client_id,
        "payload": {
            "method": "set_prop",
            "groupId": group["id"],
            "bleMeshGroupAddress": str(report["meshAddress"]),
            "params": {"OnOff": state},
            "homeId": home_id,
        },
    }
    return f"iot/v1/s/{user_id}/group/excGroupReq", message


def group_response_code(
    *, topic: str, message: Any, request: dict, user_id: str
) -> int | None:
    """Ignore unrelated traffic; validate every successful correlated reply.

    Return 200 only for acceptance of this exact target and state. A timeout
    belongs to the transport: callers must not infer physical delivery or
    automatically resend an uncertain operation.
    """
    if (
        topic != f"iot/v1/c/{user_id}/group/excGroupResp"
        or not isinstance(message, dict)
        or message.get("seq") != request["seq"]
        or message.get("method") != "excGroupResp"
        or message.get("service") != "group"
    ):
        return None
    ack = message.get("ack")
    if not isinstance(ack, dict) or type(ack.get("code")) is not int:
        raise GroupProtocolError("Correlated group reply has no integer code")
    if ack["code"] != 200:
        raise GroupRejected(ack["code"])
    echo = message.get("payload")
    expected = request["payload"]
    if not isinstance(echo, dict) or any(
        echo.get(field) != expected[field]
        for field in ("method", "groupId", "homeId", "bleMeshGroupAddress")
    ):
        raise GroupProtocolError("Accepted group reply has a different target")
    params = echo.get("params")
    if (
        not isinstance(params, dict)
        or type(params.get("OnOff")) is not int
        or params["OnOff"] != expected["params"]["OnOff"]
    ):
        raise GroupProtocolError("Accepted group reply has a different OnOff value")
    return 200
