"""Helpers for reading AiDot credentials from config entries."""

from collections.abc import Mapping
from typing import Any

from aidot.const import CONF_COUNTRY, DEFAULT_COUNTRY_CODE, SUPPORTED_COUNTRYS

_CONF_COUNTRY_CODE = "country_code"
_LEGACY_LOGIN_INFO_KEYS = ("login_info", "loginInfo")


def get_login_data(entry_data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return login data from either a flat or legacy nested config entry."""
    for key in _LEGACY_LOGIN_INFO_KEYS:
        nested_data = entry_data.get(key)
        if isinstance(nested_data, Mapping):
            return nested_data
    return entry_data


def get_country_code(entry_data: Mapping[str, Any]) -> str:
    """Return the stored country code, deriving it for older config entries."""
    login_data = get_login_data(entry_data)

    for data in (entry_data, login_data):
        country_code = data.get(_CONF_COUNTRY_CODE)
        if isinstance(country_code, str) and country_code:
            return country_code

    country_name = login_data.get(CONF_COUNTRY) or entry_data.get(CONF_COUNTRY)
    if isinstance(country_name, str):
        normalized_name = country_name.casefold()
        for country in SUPPORTED_COUNTRYS:
            if country["name"].casefold() == normalized_name:
                return country["id"]

    return DEFAULT_COUNTRY_CODE
