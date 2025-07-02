"""Config flow for AiDot integration."""
from __future__ import annotations

import logging
from typing import Any

from aidot.client import AidotClient
from aidot.exceptions import AidotAuthFailed, AidotUserOrPassIncorrect
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CLOUD_SERVERS,
    CONF_CHOOSE_HOUSE,
    CONF_MANUAL_IPS,
    CONF_PASSWORD,
    CONF_SERVER_COUNTRY,
    CONF_USE_MANUAL_IPS,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle aidot config flow."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_PUSH

    def __init__(self) -> None:
        """Initialize the config flow."""
        self.client: AidotClient | None = None
        self.login_info: dict[Any, Any] = {}
        self.house_list: list[Any] = []
        self.device_list: list[Any] = []
        self.product_list: list[Any] = []
        self.selected_house: dict[Any, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            session = async_get_clientsession(self.hass)
            self.client = AidotClient(
                session,
                user_input[CONF_SERVER_COUNTRY],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            try:
                self.login_info = await self.client.async_post_login()
                _LOGGER.debug(f"Login successful, login_info: {self.login_info}")

                # get houses
                self.house_list = await self.client.async_get_houses()
                _LOGGER.debug(f"Got houses: {self.house_list}")

                return await self.async_step_choose_house()

            except AidotUserOrPassIncorrect:
                errors["base"] = "invalid_auth"
            except AidotAuthFailed:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        if user_input is None:
            user_input = {}

        counties_name = [item["name"] for item in CLOUD_SERVERS]
        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_SERVER_COUNTRY,
                    default=user_input.get(CONF_SERVER_COUNTRY, "United States"),
                ): vol.In(counties_name),
                vol.Required(
                    CONF_USERNAME, default=user_input.get(CONF_USERNAME, vol.UNDEFINED)
                ): str,
                vol.Required(
                    CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, vol.UNDEFINED)
                ): str,
            }
        )

        return self.async_show_form(
            step_id="user", data_schema=data_schema, errors=errors
        )

    async def async_step_choose_house(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Please select a room."""
        errors: dict[str, str] = {}
        if user_input is not None and self.client:
            # get all house name
            for item in self.house_list:
                if item["name"] == user_input.get(CONF_CHOOSE_HOUSE):
                    self.selected_house = item

            # get device_list
            _LOGGER.debug(f"Selected house: {self.selected_house}")
            self.device_list = await self.client.async_get_devices(
                self.selected_house["id"]
            )
            _LOGGER.debug(f"Got devices: {self.device_list}")

            # get product_list
            if self.device_list:
                product_ids = ",".join(
                    [item["productId"] for item in self.device_list]
                )
                self.product_list = await self.client.async_get_products(product_ids)
                _LOGGER.debug(f"Got products: {self.product_list}")

            self.device_list = await self.client.async_get_devices(
                self.selected_house["id"]
            )
            _LOGGER.debug(f"Got devices: {self.device_list}")
            product_ids = [d["productId"] for d in self.device_list]
            if product_ids:
                _LOGGER.debug(
                    f"Getting product info for product ids: {product_ids}"
                )
                self.product_list = await self.client.async_get_products(product_ids)
                _LOGGER.debug(f"Got products: {self.product_list}")

            return await self.async_step_discovery_method()

        if user_input is None:
            user_input = {}

        # get default house
        default_house = {}
        for item in self.house_list:
            if item["isDefault"]:
                default_house = item
                break

        # get all house name
        house_name_list = [item["name"] for item in self.house_list]
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CHOOSE_HOUSE,
                    default=user_input.get(
                        CONF_CHOOSE_HOUSE, default_house.get("name")
                    ),
                ): vol.In(house_name_list)
            }
        )
        return self.async_show_form(
            step_id="choose_house",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_discovery_method(self, user_input=None):
        """Handle the discovery method step."""
        if user_input is not None:
            if user_input[CONF_USE_MANUAL_IPS]:
                return await self.async_step_manual_ips()

            title = self.login_info["username"] + " " + self.selected_house["name"]
            return self.async_create_entry(
                title=title,
                data={
                    "login_info": self.login_info,
                    "selected_house": self.selected_house,
                },
            )

        schema = vol.Schema({vol.Required(CONF_USE_MANUAL_IPS, default=False): bool})
        return self.async_show_form(step_id="discovery_method", data_schema=schema)

    async def async_step_manual_ips(self, user_input=None):
        """Handle the manual IP configuration step."""
        if user_input is not None:
            # Filter out empty strings
            manual_ips = {k: v for k, v in user_input.items() if v}
            title = self.login_info["username"] + " " + self.selected_house["name"]
            return self.async_create_entry(
                title=title,
                data={
                    "login_info": self.login_info,
                    "selected_house": self.selected_house,
                    CONF_MANUAL_IPS: manual_ips,
                },
            )

        schema_fields = {}
        device_names = []
        for device in self.device_list:
            device_names.append(device.get("name", device["id"]))
            # Use Optional so the field can be left blank
            schema_fields[vol.Optional(device["id"], default="")] = str

        return self.async_show_form(
            step_id="manual_ips",
            data_schema=vol.Schema(schema_fields),
            description_placeholders={"devices": ", ".join(device_names)},
        )
