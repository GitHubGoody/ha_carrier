from __future__ import annotations

import logging
import asyncio

from collections.abc import Mapping
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    TEMP_CELSIUS,
    TEMP_FAHRENHEIT,
)
from homeassistant.components.climate.const import (
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
)
from homeassistant.config_entries import ConfigEntry

from carrier_api import (
    FanModes,
    SystemModes,
    TemperatureUnits,
    ActivityNames,
    StatusZone,
    ConfigZone,
    ConfigZoneActivity,
)

from .const import DOMAIN, DATA_SYSTEMS, CONF_INFINITE_HOLDS, DEFAULT_INFINITE_HOLDS, FAN_AUTO
from .carrier_data_update_coordinator import CarrierDataUpdateCoordinator
from .carrier_entity import CarrierEntity

_LOGGER = logging.getLogger(__name__)

SUPPORT_FLAGS = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
    | ClimateEntityFeature.FAN_MODE
    | ClimateEntityFeature.PRESET_MODE
)


async def async_setup_entry(hass, config_entry: ConfigEntry, async_add_entities):
    _LOGGER.debug(f"setting up climate entry")
    infinite_hold = config_entry.options.get(
                        CONF_INFINITE_HOLDS, DEFAULT_INFINITE_HOLDS
                    )
    updaters: list[CarrierDataUpdateCoordinator] = hass.data[DOMAIN][
        config_entry.entry_id
    ][DATA_SYSTEMS]
    entities = []
    for updater in updaters:
        for zone in updater.carrier_system.config.zones:
            entities.extend(
                [
                    Thermostat(updater, infinite_hold=infinite_hold, zone_api_id=zone.api_id),
                ]
            )
    async_add_entities(entities)


class Thermostat(CarrierEntity, ClimateEntity):
    _attr_supported_features = SUPPORT_FLAGS

    def __init__(self, updater, infinite_hold: bool, zone_api_id: str):
        _LOGGER.debug(f"infinite_hold:{infinite_hold}")
        self.infinite_hold: bool = infinite_hold
        self.zone_api_id: str = zone_api_id
        self._updater = updater
        self.entity_description = ClimateEntityDescription(
            key=f"#{updater.carrier_system.serial}-zone{self.zone_api_id}-climate",
        )
        super().__init__(f"{self._status_zone.name}", updater)
        self._attr_max_temp = self._updater.carrier_system.config.limit_max
        self._attr_min_temp = self._updater.carrier_system.config.limit_min
        self._attr_fan_modes = list(map(lambda fan_mode: fan_mode.value, [FanModes.LOW, FanModes.MED, FanModes.HIGH]))
        self._attr_fan_modes.append(FAN_AUTO)
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.FAN_ONLY, HVACMode.HEAT_COOL, HVACMode.HEAT, HVACMode.COOL]
        self._attr_preset_modes = list(
            map(
                lambda activity: activity.api_id.value,
                self._config_zone.activities,
            )
        )
        self._attr_preset_modes.append('resume')

    @property
    def _status_zone(self) -> StatusZone:
        for zone in self._updater.carrier_system.status.zones:
            if zone.api_id == self.zone_api_id:
                return zone

    @property
    def _config_zone(self) -> ConfigZone:
        for zone in self._updater.carrier_system.config.zones:
            if zone.api_id == self.zone_api_id:
                return zone

    @property
    def current_humidity(self) -> int | None:
        return self._status_zone.humidity

    @property
    def current_temperature(self) -> float | None:
        return self._status_zone.temperature

    @property
    def temperature_unit(self) -> str:
        if (
            self._updater.carrier_system.status.temperature_unit
            == TemperatureUnits.FAHRENHEIT
        ):
            return TEMP_FAHRENHEIT
        else:
            return TEMP_CELSIUS

    @property
    def target_temperature(self) -> float | None:
        if self.hvac_mode == HVACMode.HEAT:
            return self.target_temperature_low
        if self.hvac_mode == HVACMode.COOL:
            return self.target_temperature_high
        return None

    @property
    def hvac_mode(self) -> HVACMode | str | None:
        ha_mode = None
        match self._updater.carrier_system.config.mode:
            case SystemModes.COOL.value:
                ha_mode = HVACMode.COOL
            case SystemModes.HEAT.value:
                ha_mode = HVACMode.HEAT
            case SystemModes.OFF.value:
                ha_mode = HVACMode.OFF
            case SystemModes.AUTO.value:
                ha_mode = HVACMode.HEAT_COOL
            case SystemModes.FAN_ONLY.value:
                ha_mode = HVACMode.FAN_ONLY
        return ha_mode

    @property
    def hvac_action(self) -> HVACAction | str | None:
        if self.hvac_mode == HVACMode.OFF:
            return HVACAction.OFF
        elif self._status_zone.conditioning == "idle":
            return HVACAction.IDLE
        elif "heat" in self._status_zone.conditioning:
            return HVACAction.HEATING
        elif "cool" in self._status_zone.conditioning:
            return HVACAction.COOLING
        elif self._status_zone.fan == FanModes.OFF:
            return HVACAction.IDLE
        else:
            return HVACAction.FAN

    def _current_activity(self) -> ConfigZoneActivity:
        return self._config_zone.current_activity()

    @property
    def target_temperature_high(self) -> float | None:
        return self._current_activity().cool_set_point

    @property
    def target_temperature_low(self) -> float | None:
        return self._current_activity().heat_set_point

    @property
    def preset_mode(self) -> str | None:
        return self._current_activity().api_id.value

    @property
    def fan_mode(self) -> str | None:
        if self._current_activity().fan == FanModes.OFF:
            return FAN_AUTO
        else:
            return self._current_activity().fan.value

    def refresh(self):
        asyncio.run_coroutine_threadsafe(asyncio.sleep(5), self.hass.loop).result()
        asyncio.run_coroutine_threadsafe(
            self._updater.async_request_refresh(), self.hass.loop
        ).result()

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        _LOGGER.debug(f"set_hvac_mode; hvac_mode:{hvac_mode}")
        if hvac_mode in [HVACMode.DRY, HVACMode.AUTO]:
            return
        match hvac_mode:
            case HVACMode.COOL:
                mode = SystemModes.COOL
            case HVACMode.HEAT:
                mode = SystemModes.HEAT
            case HVACMode.OFF:
                mode = SystemModes.OFF
            case HVACMode.HEAT_COOL:
                mode = SystemModes.AUTO
            case HVACMode.FAN_ONLY:
                mode = SystemModes.FAN_ONLY
        self._updater.carrier_system.config.mode = mode.value
        self._updater.carrier_system.api_connection.set_config_mode(
            system_serial=self._updater.carrier_system.serial, mode=mode.value
        )
        self.refresh()

    def set_preset_mode(self, preset_mode: str) -> None:
        _LOGGER.debug(f"set_preset_mode; preset_mode:{preset_mode}")
        if preset_mode == "resume":
            self._updater.carrier_system.api_connection.resume_schedule(
                system_serial=self._updater.carrier_system.serial,
                zone_id=self.zone_api_id,
            )
        else:
            activity_name = ActivityNames(preset_mode.strip().lower())
            if self.infinite_hold:
                hold_until = None
            else:
                hold_until = self._config_zone.next_activity_time()
            _LOGGER.debug(f"infinite_hold:{self.infinite_hold}; holding until:'{hold_until}'")
            self._config_zone.hold = True
            self._config_zone.hold_activity = activity_name
            self._updater.carrier_system.api_connection.set_config_hold(
                system_serial=self._updater.carrier_system.serial,
                zone_id=self.zone_api_id,
                activity_name=activity_name,
                hold_until=hold_until,
            )
        self.refresh()

    def set_fan_mode(self, fan_mode: str) -> None:
        _LOGGER.debug(f"set_fan_mode; fan_mode:{fan_mode}")
        fan_mode = FanModes(fan_mode)
        heat_set_point = self._current_activity().heat_set_point
        cool_set_point = self._current_activity().cool_set_point
        manual_activity = self._config_zone.find_activity(ActivityNames.MANUAL)
        manual_activity.heat_set_point = heat_set_point
        manual_activity.cool_set_point = cool_set_point
        manual_activity.fan = fan_mode

        self._updater.carrier_system.api_connection.set_config_manual_activity(
            system_serial=self._updater.carrier_system.serial,
            zone_id=self.zone_api_id,
            heat_set_point=heat_set_point,
            cool_set_point=cool_set_point,
            fan_mode=fan_mode,
        )
        self.refresh()

    def set_temperature(self, **kwargs) -> None:
        _LOGGER.debug(f"set_temperature; kwargs:{kwargs}")
        heat_set_point = kwargs.get(ATTR_TARGET_TEMP_LOW)
        cool_set_point = kwargs.get(ATTR_TARGET_TEMP_HIGH)
        temperature = kwargs.get(ATTR_TEMPERATURE)

        if self._updater.carrier_system.config.mode == SystemModes.COOL.value:
            heat_set_point = self.min_temp
            cool_set_point = temperature or cool_set_point
        elif self._updater.carrier_system.config.mode == SystemModes.HEAT.value:
            heat_set_point = temperature or heat_set_point
            cool_set_point = self.max_temp

        if self.temperature_unit == TEMP_FAHRENHEIT:
            heat_set_point = int(heat_set_point)
            cool_set_point = int(cool_set_point)

        manual_activity = self._config_zone.find_activity(ActivityNames.MANUAL)
        fan_mode = manual_activity.fan
        manual_activity.cool_set_point = cool_set_point
        manual_activity.heat_set_point = heat_set_point

        _LOGGER.debug(
            f"set_temperature; heat_set_point:{heat_set_point}, cool_set_point:{cool_set_point}, fan_mode:{fan_mode}"
        )
        self._updater.carrier_system.api_connection.set_config_manual_activity(
            system_serial=self._updater.carrier_system.serial,
            zone_id=self.zone_api_id,
            heat_set_point=heat_set_point,
            cool_set_point=cool_set_point,
            fan_mode=fan_mode,
        )
        self.refresh()

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        return {
            "airflow_cfm": self._updater.carrier_system.status.airflow_cfm,
            "status_mode": self._updater.carrier_system.status.mode,
        }
