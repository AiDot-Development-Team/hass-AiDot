"""Tests for config-entry credential compatibility helpers."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aidot.exceptions import AidotAuthFailed, AidotUserOrPassIncorrect
from aidot.client import AidotClient
from aiohttp import ClientResponseError
from yarl import URL
import pytest

_AUTH_PATH = (
    Path(__file__).resolve().parent.parent / "custom_components" / "aidot" / "auth.py"
)
_AUTH_SPEC = importlib.util.spec_from_file_location("aidot_auth", _AUTH_PATH)
assert _AUTH_SPEC is not None and _AUTH_SPEC.loader is not None
auth = importlib.util.module_from_spec(_AUTH_SPEC)
_AUTH_SPEC.loader.exec_module(auth)


def test_get_login_data_supports_legacy_snake_case() -> None:
    """The v1.0.8 login_info wrapper is unwrapped."""
    login_data = {"username": "user@example.com", "country": "United States"}

    assert auth.get_login_data({"login_info": login_data}) is login_data


def test_get_login_data_supports_legacy_camel_case() -> None:
    """The python-aidot loginInfo spelling is also unwrapped."""
    login_data = {"username": "user@example.com", "country": "United States"}

    assert auth.get_login_data({"loginInfo": login_data}) is login_data


def test_get_country_code_uses_current_flat_value() -> None:
    """Current entries retain their explicitly selected country code."""
    assert auth.get_country_code({"country_code": "CA", "country": "Canada"}) == "CA"


def test_get_country_code_derives_legacy_value() -> None:
    """Legacy entries recover their country code from the stored country name."""
    assert (
        auth.get_country_code(
            {
                "login_info": {
                    "username": "user@example.com",
                    "country": "United States",
                }
            }
        )
        == "US"
    )


async def test_valid_session_does_not_log_in_again() -> None:
    devices = {"device_list": []}
    client = SimpleNamespace(
        async_get_all_device=AsyncMock(return_value=devices),
        async_post_login=AsyncMock(),
    )
    save = Mock()
    assert await auth.async_get_devices_with_relogin(client, save) is devices
    client.async_post_login.assert_not_awaited()
    save.assert_not_called()


async def test_invalid_cloud_session_logs_in_once_and_saves_before_retry() -> None:
    devices = {"device_list": [{"id": "light"}]}
    saved = False

    def save():
        nonlocal saved
        saved = True

    async def fetch():
        if not saved:
            raise AidotAuthFailed()
        return devices

    client = SimpleNamespace(
        async_get_all_device=AsyncMock(side_effect=fetch),
        async_post_login=AsyncMock(),
    )
    assert await auth.async_get_devices_with_relogin(client, save) is devices
    client.async_post_login.assert_awaited_once()
    assert client.async_get_all_device.await_count == 2


async def test_bad_password_propagates_to_home_assistant_reauth() -> None:
    client = SimpleNamespace(
        async_get_all_device=AsyncMock(side_effect=AidotAuthFailed()),
        async_post_login=AsyncMock(side_effect=AidotUserOrPassIncorrect()),
    )
    save = Mock()
    with pytest.raises(AidotUserOrPassIncorrect):
        await auth.async_get_devices_with_relogin(client, save)
    save.assert_not_called()
    assert client.async_get_all_device.await_count == 1


async def test_repeated_invalid_session_does_not_loop() -> None:
    client = SimpleNamespace(
        async_get_all_device=AsyncMock(side_effect=AidotAuthFailed()),
        async_post_login=AsyncMock(),
    )
    with pytest.raises(AidotAuthFailed):
        await auth.async_get_devices_with_relogin(client, Mock())
    client.async_post_login.assert_awaited_once()
    assert client.async_get_all_device.await_count == 2


def response_error(status, path):
    return ClientResponseError(
        SimpleNamespace(real_url=URL("https://prod-us-api.arnoo.com/v17" + path)),
        (), status=status,
    )


@pytest.mark.parametrize("status", [400, 401])
async def test_real_library_rejected_refresh_recovers_without_reload(monkeypatch, status):
    """0.3.56 propagates raw refresh HTTP errors instead of AidotAuthFailed."""
    class Response:
        def __init__(self, body, error=None):
            self.body, self.error = body, error

        async def json(self):
            return self.body

        def raise_for_status(self):
            if self.error:
                raise self.error

    old_token = {
        "id": "user1", "username": "test@example.com", "password": "test-password",
        "region": "us", "country": "United States", "accessToken": "old-access",
        "refreshToken": "old-refresh",
    }
    new_token = {**old_token, "accessToken": "new-access", "refreshToken": "new-refresh"}
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[
            Response({"code": 21026}, response_error(401, "/houses")), Response([]),
        ]),
        post=AsyncMock(side_effect=[
            Response({"code": 21026}, response_error(status, "/users/refreshToken")),
            Response(new_token.copy()),
        ]),
    )
    monkeypatch.setattr(AidotClient, "setup_discover", lambda self: None)
    client = AidotClient(session=session, token=old_token)
    client.get_terminal_id = AsyncMock(return_value="test-terminal")
    saved = []
    result = await auth.async_get_devices_with_relogin(
        client, lambda: saved.append(client.login_info.copy())
    )
    assert result == {"device_list": []}
    assert len(saved) == 1 and saved[0]["accessToken"] == "new-access"
    assert session.get.await_count == 2
    assert session.get.await_args_list[-1].kwargs["headers"]["Token"] == "new-access"
    assert session.post.await_count == 2


@pytest.mark.parametrize("status,path", [(500, "/houses"), (400, "/devices")])
async def test_non_auth_http_errors_do_not_trigger_password_login(status, path):
    client = SimpleNamespace(
        async_get_all_device=AsyncMock(side_effect=response_error(status, path)),
        async_post_login=AsyncMock(),
    )
    with pytest.raises(ClientResponseError):
        await auth.async_get_devices_with_relogin(client, Mock())
    client.async_post_login.assert_not_awaited()
