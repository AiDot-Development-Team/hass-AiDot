"""Exercise the actual light platform without requiring a Home Assistant install."""

import importlib.util
import sys
from enum import IntFlag, StrEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def light_module(monkeypatch):
    """Stub HA's entity plumbing, retaining the platform's command/state code."""
    class ColorMode(StrEnum):
        RGBW = "rgbw"
        COLOR_TEMP = "color_temp"
        BRIGHTNESS = "brightness"

    class LightEntityFeature(IntFlag):
        EFFECT = 4

    class LightEntity:
        _attr_supported_features = 0

        def async_write_ha_state(self):
            self.writes = getattr(self, "writes", 0) + 1

    class CoordinatorEntity:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, coordinator):
            self.coordinator = coordinator

        @property
        def available(self):
            return True

        def _handle_coordinator_update(self):
            self.async_write_ha_state()

    modules = {
        "homeassistant": {},
        "homeassistant.components.light": {
            "ATTR_BRIGHTNESS": "brightness",
            "ATTR_COLOR_TEMP_KELVIN": "color_temp_kelvin",
            "ATTR_EFFECT": "effect",
            "ATTR_RGBW_COLOR": "rgbw_color",
            "ColorMode": ColorMode,
            "LightEntity": LightEntity,
            "LightEntityFeature": LightEntityFeature,
        },
        "homeassistant.core": {"HomeAssistant": object, "callback": lambda f: f},
        "homeassistant.helpers.device_registry": {
            "CONNECTION_NETWORK_MAC": "mac", "DeviceInfo": dict,
        },
        "homeassistant.helpers.entity_platform": {
            "AddConfigEntryEntitiesCallback": object,
        },
        "homeassistant.helpers.update_coordinator": {
            "CoordinatorEntity": CoordinatorEntity,
        },
        "light_platform_test.coordinator": {
            "AidotConfigEntry": object, "AidotDeviceUpdateCoordinator": object,
        },
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    root = Path(__file__).resolve().parent.parent / "custom_components" / "aidot"
    package = ModuleType("light_platform_test")
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    spec = importlib.util.spec_from_file_location("light_platform_test.light", root / "light.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_light(module, *, on=True):
    status = SimpleNamespace(
        online=True, on=on, dimming=58, cct=4000, rgbw=(0, 0, 255, 0),
        active_color_mode="rgbw", effect_mode=None,
    )
    info = SimpleNamespace(
        dev_id="light1", model_id="LK.A000181", mac="AA:BB", name="Test",
        hw_version=None, enable_rgbw=True, enable_cct=True, effects={},
    )
    client = SimpleNamespace(
        info=info, status=status, send_dev_attr=AsyncMock(),
        async_set_brightness=AsyncMock(), async_set_rgbw=AsyncMock(),
        async_set_cct=AsyncMock(), async_set_effect=AsyncMock(),
        async_turn_on=AsyncMock(), async_turn_off=AsyncMock(),
    )
    coordinator = SimpleNamespace(device_client=client, data=status)
    return module.AidotLight(coordinator), client


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.parametrize("kwargs, expected, mode", [
    ({"rgbw_color": (0, 255, 0, 0), "brightness": 255},
     {"OnOff": 1, "RGBW": 16711680, "Dimming": 100}, "rgbw"),
    ({"rgbw_color": (255, 90, 0, 0), "brightness": 200},
     {"OnOff": 1, "RGBW": -10878976, "Dimming": 78}, "rgbw"),
    ({"rgbw_color": (255, 0, 0, 0), "brightness": 128},
     {"OnOff": 1, "RGBW": -16777216, "Dimming": 50}, "rgbw"),
    ({"color_temp_kelvin": 2700, "brightness": 255},
     {"OnOff": 1, "CCT": 2700, "Dimming": 100}, "color_temp"),
])
async def test_colour_and_brightness_are_one_complete_command(light_module, on, kwargs, expected, mode):
    light, client = make_light(light_module, on=on)
    await light.async_turn_on(**kwargs)
    client.send_dev_attr.assert_awaited_once_with(expected)
    assert light._attr_is_on is True
    assert light._attr_color_mode == mode
    assert light._attr_brightness == int(expected["Dimming"] * 255 / 100)
    if "rgbw_color" in kwargs:
        assert light._attr_rgbw_color == kwargs["rgbw_color"]
    else:
        assert light._attr_color_temp_kelvin == 2700


async def test_plain_on_does_not_resend_stale_colour_or_brightness(light_module):
    light, client = make_light(light_module, on=False)
    await light.async_turn_on()
    client.send_dev_attr.assert_awaited_once_with({"OnOff": 1})


async def test_failed_compound_command_does_not_claim_new_state(light_module):
    light, client = make_light(light_module, on=False)
    client.send_dev_attr.side_effect = ConnectionError("hub rejected command")
    with pytest.raises(ConnectionError):
        await light.async_turn_on(rgbw_color=(255, 0, 0, 0), brightness=255)
    assert light._attr_is_on is False
    assert light._attr_rgbw_color == (0, 0, 255, 0)
    assert not getattr(light, "writes", 0)


async def test_telemetry_colour_mode_is_applied(light_module):
    light, client = make_light(light_module)
    client.status.active_color_mode = "color_temp"
    client.status.cct = 2700
    light._handle_coordinator_update()
    assert light._attr_color_mode == "color_temp"


async def test_effects_are_advertised_when_metadata_has_them(light_module):
    light, client = make_light(light_module)
    client.info.effects = {"Rainbow": 5}
    light = light_module.AidotLight(light.coordinator)
    assert light._attr_supported_features & light_module.LightEntityFeature.EFFECT
    await light.async_turn_on(effect="Rainbow", brightness=128)
    client.send_dev_attr.assert_awaited_once_with({"OnOff": 1, "EffectMode": 5, "Dimming": 50})


async def test_wifi_preset_uses_real_upstream_primitive_api(light_module):
    """Keep python-aidot 0.3.58's named presets distinct from BLE mode codes."""
    from aidot.device_client import DeviceClient

    light, client = make_light(light_module, on=False)
    primitive = SimpleNamespace(primitiveEffectId="preset-id")
    client.info.presets = {"Sunset": primitive}
    client.info.preset_names = ["Sunset"]
    client.client = SimpleNamespace(async_execute_diff_command=AsyncMock())
    client.device_id = "light1"
    client.async_set_effect = DeviceClient.async_set_effect.__get__(client)
    light = light_module.AidotLight(light.coordinator)

    await light.async_turn_on(effect="Sunset", brightness=128)

    client.send_dev_attr.assert_awaited_once_with({"OnOff": 1, "Dimming": 50})
    client.client.async_execute_diff_command.assert_awaited_once_with(
        device_id="light1", primitive=primitive
    )
    assert light._attr_effect_list == ["Sunset"]
    assert light._attr_effect == "Sunset"
    assert light._attr_is_on is True


async def test_static_colour_clears_wifi_preset_state(light_module):
    light, client = make_light(light_module)
    client.info.preset_names = ["Sunset"]
    client.status.effect = "Sunset"
    light = light_module.AidotLight(light.coordinator)
    assert light._attr_effect == "Sunset"

    await light.async_turn_on(rgbw_color=(0, 255, 0, 0), brightness=255)

    assert light._attr_effect is None
    assert client.status.effect == ""
    client.async_set_effect.assert_not_awaited()


async def test_wifi_preset_telemetry_updates_effect(light_module):
    light, client = make_light(light_module)
    client.info.preset_names = ["Sunset"]
    client.status.effect = ""
    light = light_module.AidotLight(light.coordinator)
    client.status.effect = "Sunset"
    light._handle_coordinator_update()
    assert light._attr_effect == "Sunset"


async def test_failed_wifi_preset_does_not_claim_success(light_module):
    light, client = make_light(light_module, on=False)
    client.info.preset_names = ["Sunset"]
    client.async_set_effect.side_effect = ConnectionError("preset request failed")
    light = light_module.AidotLight(light.coordinator)

    with pytest.raises(ConnectionError):
        await light.async_turn_on(effect="Sunset", brightness=128)

    assert light._attr_is_on is False
    assert not getattr(light, "writes", 0)
