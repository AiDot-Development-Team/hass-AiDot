"""Binary sensor support for AiDot BLE mesh telemetry."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .ble_gateway_client import BleGatewayDeviceClient
from .const import DOMAIN
from .coordinator import AidotConfigEntry, AidotDeviceUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class AidotBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe an AiDot binary telemetry sensor."""

    status_key: str
    api_attribute: str | None = None
    always_create: bool = False


BINARY_SENSORS = (
    AidotBinarySensorEntityDescription(
        key="battery_charging",
        translation_key="battery_charging",
        status_key="charging",
        api_attribute="chargeState",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AidotBinarySensorEntityDescription(
        key="telemetry_connectivity",
        translation_key="telemetry_connectivity",
        status_key="telemetry_connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        always_create=True,
    ),
    AidotBinarySensorEntityDescription(
        key="energy_saving_enabled",
        translation_key="energy_saving_enabled",
        status_key="energy_saving_enabled",
        api_attribute="energySavingEnable",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AidotBinarySensorEntityDescription(
        key="security_alarm",
        translation_key="security_alarm",
        status_key="security_alarm",
        api_attribute="SecurityAlarm",
        device_class=BinarySensorDeviceClass.TAMPER,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    AidotBinarySensorEntityDescription(
        key="alarm_status",
        translation_key="alarm_status",
        status_key="alarm_status",
        api_attribute="alarmStatus",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AiDot BLE binary telemetry sensors."""
    del hass
    entities: list[AidotBleBinarySensor] = []
    for coordinator in entry.runtime_data.device_coordinators.values():
        client = coordinator.device_client
        if not isinstance(client, BleGatewayDeviceClient):
            continue
        for description in BINARY_SENSORS:
            if description.always_create or getattr(
                coordinator.data, description.status_key, None
            ) is not None or (
                description.api_attribute is not None
                and client.supports_telemetry_attribute(description.api_attribute)
            ):
                entities.append(AidotBleBinarySensor(coordinator, description))
    async_add_entities(entities)


class AidotBleBinarySensor(
    CoordinatorEntity[AidotDeviceUpdateCoordinator], BinarySensorEntity
):
    """Representation of an AiDot BLE mesh binary telemetry sensor."""

    _attr_has_entity_name = True
    entity_description: AidotBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotBinarySensorEntityDescription,
    ) -> None:
        """Initialize a binary telemetry sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        info = coordinator.device_client.info
        self._attr_unique_id = f"{info.dev_id}_{description.key}"

        manufacturer = info.model_id.split(".")[0]
        model = info.model_id[len(manufacturer) + 1 :]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, info.dev_id)},
            connections={(CONNECTION_NETWORK_MAC, info.mac)},
            manufacturer=manufacturer,
            model=model,
            name=info.name,
            hw_version=info.hw_version,
        )
        self._update_status()

    def _update_status(self) -> None:
        """Update the binary state from coordinator memory."""
        self._attr_is_on = getattr(
            self.coordinator.data, self.entity_description.status_key, None
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated telemetry."""
        self._update_status()
        super()._handle_coordinator_update()
