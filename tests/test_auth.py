"""Tests for config-entry credential compatibility helpers."""

import importlib.util
from pathlib import Path

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
