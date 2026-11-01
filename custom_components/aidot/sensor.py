"""Sensor support for AiDot BLE mesh telemetry."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfTime,
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
class AidotSensorEntityDescription(SensorEntityDescription):
    """Describe an AiDot telemetry sensor."""

    status_key: str
    api_attribute: str | None = None
    always_create: bool = False


SENSORS = (
    AidotSensorEntityDescription(
        key="battery",
        translation_key="battery",
        status_key="battery",
        api_attribute="Battery_remaining",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AidotSensorEntityDescription(
        key="mesh_signal_strength",
        translation_key="mesh_signal_strength",
        status_key="mesh_rssi",
        api_attribute="meshNetRssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    AidotSensorEntityDescription(
        key="last_telemetry_update",
        translation_key="last_telemetry_update",
        status_key="last_telemetry_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        always_create=True,
    ),
    AidotSensorEntityDescription(
        key="energy_saving_factor",
        translation_key="energy_saving_factor",
        status_key="energy_saving_factor",
        api_attribute="energySavingFactor",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AidotSensorEntityDescription(
        key="light_duration",
        translation_key="light_duration",
        status_key="light_duration",
        api_attribute="lightDuration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    AidotSensorEntityDescription(
        key="detection_mode",
        translation_key="detection_mode",
        status_key="detection_mode",
        api_attribute="DetectionMode",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AidotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up AiDot BLE telemetry sensors."""
    del hass
    entities: list[AidotBleTelemetrySensor] = []
    for coordinator in entry.runtime_data.device_coordinators.values():
        client = coordinator.device_client
        if not isinstance(client, BleGatewayDeviceClient):
            continue
        for description in SENSORS:
            if description.always_create or getattr(
                coordinator.data, description.status_key, None
            ) is not None or (
                description.api_attribute is not None
                and client.supports_telemetry_attribute(description.api_attribute)
            ):
                entities.append(AidotBleTelemetrySensor(coordinator, description))
    async_add_entities(entities)


class AidotBleTelemetrySensor(
    CoordinatorEntity[AidotDeviceUpdateCoordinator], SensorEntity
):
    """Representation of an AiDot BLE mesh telemetry sensor."""

    _attr_has_entity_name = True
    entity_description: AidotSensorEntityDescription

    def __init__(
        self,
        coordinator: AidotDeviceUpdateCoordinator,
        description: AidotSensorEntityDescription,
    ) -> None:
        """Initialize a telemetry sensor."""
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
        """Update the sensor value from coordinator memory."""
        self._attr_native_value = getattr(
            self.coordinator.data, self.entity_description.status_key, None
        )

    @property
    def available(self) -> bool:
        """Return whether the light is available."""
        return super().available and self.coordinator.data.online

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated telemetry."""
        self._update_status()
        super()._handle_coordinator_update()
