"""Config flow for Aidot integration."""

from typing import Any, override

import probatio
from aiohttp import ClientError
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_COUNTRY_CODE, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from aidot.client import AidotClient
from aidot.const import CONF_ID, DEFAULT_COUNTRY_CODE, SUPPORTED_COUNTRY_CODES
from aidot.exceptions import AidotUserOrPassIncorrect

from .const import (
    CONF_EFFECT_SELECTION,
    CONF_EFFECT_SOURCE,
    DEFAULT_EFFECT_SOURCE,
    DOMAIN,
    EFFECT_SOURCE_ALL,
    EFFECT_SOURCE_MANUAL,
    EFFECT_SOURCE_RECOMMENDED,
)

CONF_MANUAL_DEVICE = "manual_device"
STEP_CONFIGURE_ANOTHER_LIGHT = "configure_another_light"
STEP_FINISH_MANUAL_SELECTION = "finish_manual_selection"

DATA_SCHEMA = probatio.Schema(
    {
        probatio.Required(
            CONF_COUNTRY_CODE,
            default=DEFAULT_COUNTRY_CODE,
        ): selector.CountrySelector(
            selector.CountrySelectorConfig(
                countries=SUPPORTED_COUNTRY_CODES,
            )
        ),
        probatio.Required(CONF_USERNAME): str,
        probatio.Required(CONF_PASSWORD): str,
    }
)


def _effect_source_schema(
    default: str = DEFAULT_EFFECT_SOURCE, *, include_manual: bool = False
) -> probatio.Schema:
    """Return schema for effect source selection."""
    options = [
        selector.SelectOptionDict(
            value=EFFECT_SOURCE_ALL,
            label="All effects",
        ),
        selector.SelectOptionDict(
            value=EFFECT_SOURCE_RECOMMENDED,
            label="Recommended effects",
        ),
    ]
    if include_manual:
        options.append(
            selector.SelectOptionDict(
                value=EFFECT_SOURCE_MANUAL,
                label="Manually selected effects",
            )
        )

    return probatio.Schema(
        {
            probatio.Required(
                CONF_EFFECT_SOURCE,
                default=default,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


EFFECT_SOURCE_SCHEMA = _effect_source_schema()


class AidotOptionsFlowHandler(OptionsFlow):
    """Handle Aidot options flow."""

    _options: dict[str, Any]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage Aidot options."""
        if user_input is not None:
            self._options = dict(self.config_entry.options)
            self._options[CONF_EFFECT_SOURCE] = user_input[CONF_EFFECT_SOURCE]
            if user_input[CONF_EFFECT_SOURCE] == EFFECT_SOURCE_MANUAL:
                return await self.async_step_manual_device()
            return self._async_update_options(self._options)

        return self.async_show_form(
            step_id="init",
            data_schema=_effect_source_schema(
                self.config_entry.options.get(CONF_EFFECT_SOURCE, DEFAULT_EFFECT_SOURCE),
                include_manual=True,
            ),
            errors={},
        )

    async def async_step_manual_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a device for manual effect selection."""
        if not hasattr(self, "_options"):
            self._options = dict(self.config_entry.options)
            self._options[CONF_EFFECT_SOURCE] = EFFECT_SOURCE_MANUAL

        if user_input is not None:
            self._manual_device_id = user_input[CONF_MANUAL_DEVICE]
            return await self.async_step_manual_effects()

        return self.async_show_form(
            step_id="manual_device",
            data_schema=_manual_device_schema(
                getattr(self.config_entry, "runtime_data", None)
            ),
            errors={},
        )

    async def async_step_manual_effects(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage manually selected effects."""
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if user_input is not None:
            effect_selection = dict(self._options.get(CONF_EFFECT_SELECTION, {}))
            effect_selection[self._manual_device_id] = user_input[
                CONF_EFFECT_SELECTION
            ]
            self._options[CONF_EFFECT_SELECTION] = effect_selection
            return await self.async_step_manual_menu()

        return self.async_show_form(
            step_id="manual_effects",
            data_schema=_manual_effects_schema(
                coordinator,
                self._manual_device_id,
                self._options.get(CONF_EFFECT_SELECTION, {}),
            ),
            errors={},
        )

    async def async_step_manual_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose whether to configure another light or finish."""
        return self.async_show_menu(
            step_id="manual_menu",
            menu_options=[
                STEP_CONFIGURE_ANOTHER_LIGHT,
                STEP_FINISH_MANUAL_SELECTION,
            ],
        )

    async def async_step_configure_another_light(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure another light."""
        return await self.async_step_manual_device()

    async def async_step_finish_manual_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save manual effect selection."""
        return self._async_update_options(self._options)

    def _async_update_options(self, options: dict[str, Any]) -> ConfigFlowResult:
        """Update options and refresh runtime data."""
        if (coordinator := getattr(self.config_entry, "runtime_data", None)) is not None:
            coordinator.update_options(options)
        return self.async_create_entry(title="", data=options)


class AidotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle aidot config flow."""

    _login_info: dict[str, Any]
    _title: str

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> AidotOptionsFlowHandler:
        """Get the options flow for this handler."""
        return AidotOptionsFlowHandler()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client = AidotClient(
                session=async_get_clientsession(self.hass),
                country_code=user_input[CONF_COUNTRY_CODE],
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
            )
            try:
                login_info = await client.async_post_login()
            except AidotUserOrPassIncorrect:
                errors["base"] = "invalid_auth"
            except TimeoutError, ClientError:
                errors["base"] = "cannot_connect"

            if not errors:
                await self.async_set_unique_id(login_info[CONF_ID])
                self._abort_if_unique_id_configured()
                self._login_info = login_info
                self._title = (
                    f"{user_input[CONF_USERNAME]} {user_input[CONF_COUNTRY_CODE]}"
                )
                return await self.async_step_effect_source()

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )

    async def async_step_effect_source(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle effect source selection."""
        if user_input is not None:
            return self.async_create_entry(
                title=self._title,
                data=self._login_info,
                options={
                    CONF_EFFECT_SOURCE: user_input[CONF_EFFECT_SOURCE],
                    CONF_EFFECT_SELECTION: {},
                },
            )

        return self.async_show_form(
            step_id="effect_source",
            data_schema=EFFECT_SOURCE_SCHEMA,
            errors={},
        )


def _manual_device_schema(coordinator: Any) -> probatio.Schema:
    """Return schema for manual device selection."""
    if coordinator is None:
        return probatio.Schema({})

    return probatio.Schema(
        {
            probatio.Required(CONF_MANUAL_DEVICE): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=_manual_device_options(coordinator),
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
        }
    )


def _manual_device_options(coordinator: Any) -> list[selector.SelectOptionDict]:
    """Return manual device choices."""
    options: list[selector.SelectOptionDict] = []
    options.extend(
        selector.SelectOptionDict(
            value=dev_id,
            label=device_coordinator.device_client.info.name,
        )
        for dev_id, device_coordinator in coordinator.device_coordinators.items()
        if _manual_effects_for_device(coordinator, dev_id)
    )
    return options


def _manual_effects_schema(
    coordinator: Any, dev_id: str, effect_selection: dict[str, list[str]]
) -> probatio.Schema:
    """Return schema for manual effect selection."""
    if coordinator is None:
        return probatio.Schema({})

    effects = _manual_effects_for_device(coordinator, dev_id)
    return probatio.Schema(
        {
            probatio.Optional(
                CONF_EFFECT_SELECTION, default=effect_selection.get(dev_id, [])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=effect_id,
                            label=effect_name,
                        )
                        for effect_name, effect_id in _manual_effect_options(effects)
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    multiple=True,
                )
            )
        }
    )


def _manual_effects_for_device(coordinator: Any, dev_id: str) -> dict[str, Any]:
    """Return all effects available for manual selection."""
    device = coordinator.devices_by_id.get(dev_id)
    if device is not None:
        effects = coordinator.client.get_all_cached_effects(device)
        if effects:
            return effects

    if (device_coordinator := coordinator.device_coordinators.get(dev_id)) is None:
        return {}
    return device_coordinator.device_client.info.presets


def _manual_effect_options(effects: dict[str, Any]) -> list[tuple[str, str]]:
    """Return effect labels and ids for manual selection."""
    options: list[tuple[str, str]] = []
    for effect_name, effect in effects.items():
        effect_id = effect.favoriteId or effect.primitiveEffectId
        if effect_id:
            options.append((effect_name, effect_id))
    return options
