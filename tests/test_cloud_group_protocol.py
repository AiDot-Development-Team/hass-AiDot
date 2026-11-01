"""Cloud replies must confirm the requested group rather than unrelated traffic."""

from copy import deepcopy

import pytest

from tools.cloud_group_protocol import (
    GroupProtocolError,
    GroupRejected,
    build_on_off_request,
    group_response_code,
)
from tools.mesh_groups import GroupInventoryError


def request_args():
    return {
        "group": {
            "id": "group-one", "bleMeshGroupType": "normal",
            "bleMeshGroupAddress": "49289", "deviceIds": ["light-one"],
        },
        "devices": [
            {"id": "hub-one", "type": "BleMesh_Hub"},
            {"id": "light-one", "type": "light", "bleMeshDeviceKey": "secret",
             "directGateway": "hub-one"},
        ],
        "home_id": "home-one", "user_id": "user-one",
        "client_id": "session-user-one", "state": 0,
        "sequence": "unique-sequence", "unix_time": 1793476423,
    }


def reply_for(request):
    return {
        "method": "excGroupResp", "service": "group", "seq": request["seq"],
        "ack": {"code": 200}, "payload": deepcopy(request["payload"]),
    }


def validate(request, reply, topic="iot/v1/c/user-one/group/excGroupResp"):
    return group_response_code(topic=topic, message=reply, request=request, user_id="user-one")


def test_build_one_group_request_without_device_keys_or_individual_fanout():
    args = request_args()
    before = deepcopy(args)
    topic, request = build_on_off_request(**args)
    assert topic == "iot/v1/s/user-one/group/excGroupReq"
    assert request["payload"] == {
        "method": "set_prop", "groupId": "group-one",
        "bleMeshGroupAddress": "49289", "params": {"OnOff": 0}, "homeId": "home-one",
    }
    assert "secret" not in str(request)
    assert args == before
    assert validate(request, reply_for(request)) == 200


@pytest.mark.parametrize("field,value", [
    ("state", True), ("state", 2), ("state", "0"),
    ("user_id", "other/#"), ("home_id", ""),
    ("client_id", "session-other-user"), ("unix_time", True),
])
def test_refuse_invalid_requests_before_transport(field, value):
    args = request_args()
    args[field] = value
    with pytest.raises(GroupProtocolError):
        build_on_off_request(**args)


def test_refuse_group_with_unknown_member_instead_of_narrowing_it():
    args = request_args()
    args["group"]["deviceIds"].append("unknown-member")
    with pytest.raises(GroupInventoryError):
        build_on_off_request(**args)


@pytest.mark.parametrize("change", ["sequence", "service", "method", "topic", "not-object"])
def test_ignore_unrelated_messages(change):
    _, request = build_on_off_request(**request_args())
    reply = reply_for(request)
    topic = "iot/v1/c/user-one/group/excGroupResp"
    if change == "topic":
        topic = "iot/v1/c/other-user/group/excGroupResp"
    elif change == "not-object":
        reply = []
    else:
        reply[{"sequence": "seq", "service": "service", "method": "method"}[change]] = "unrelated"
    assert validate(request, reply, topic) is None


@pytest.mark.parametrize("field,value", [
    ("groupId", "other-group"), ("homeId", "other-home"),
    ("bleMeshGroupAddress", "65535"), ("method", "other-operation"),
    ("params", {"OnOff": 1}), ("params", {"OnOff": False}),
])
def test_reject_success_ack_with_different_target_or_state(field, value):
    _, request = build_on_off_request(**request_args())
    reply = reply_for(request)
    reply["payload"][field] = value
    with pytest.raises(GroupProtocolError):
        validate(request, reply)


def test_correlated_rejection_reports_only_code():
    _, request = build_on_off_request(**request_args())
    reply = reply_for(request)
    reply["ack"] = {"code": 403, "desc": "private upstream error details"}
    with pytest.raises(GroupRejected) as error:
        validate(request, reply)
    assert error.value.code == 403
    assert "private" not in str(error.value)
