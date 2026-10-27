"""Typed snapshots returned by LambdatronicClient."""

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import time
from typing import TYPE_CHECKING

from .texts import FAULT_BOILER_STATES

if TYPE_CHECKING:
    from _typeshed import DataclassInstance

MINUTES_PER_DAY = 1440

type FlatValue = float | int | bool | time | None
"""A single value under a full key such as 'hc1_flow_temperature' (see `flat()`)."""

# Key prefix of the numbered components, e.g. heating circuit 2 -> "hc2_".
_COMPONENT_PREFIXES = {"heating_circuits": "hc", "hot_water": "dhw", "buffers": "buffer"}
_NOT_FLAT = frozenset({"errors", "raw"})


def minutes_to_time(minutes: int) -> time:
    """Convert minutes after midnight (0..1439) to a time of day."""
    if not 0 <= minutes < MINUTES_PER_DAY:
        raise ValueError(f"{minutes} minutes is outside 0..{MINUTES_PER_DAY - 1}")
    return time(minutes // 60, minutes % 60)


def time_to_minutes(value: time) -> int:
    """Convert a time of day to minutes after midnight; seconds are dropped."""
    return value.hour * 60 + value.minute


def _flatten(item: "DataclassInstance", prefix: str = "") -> dict[str, FlatValue]:
    flat: dict[str, FlatValue] = {}
    for f in fields(item):
        value = getattr(item, f.name)
        if f.name in _NOT_FLAT:
            continue
        if f.name in _COMPONENT_PREFIXES:
            for number, component in value.items():
                flat.update(_flatten(component, f"{_COMPONENT_PREFIXES[f.name]}{number}_"))
        elif f.name == "circulation":
            if value is not None:
                flat.update(_flatten(value, "circulation_"))
        else:
            flat[prefix + f.name] = value
    return flat


@dataclass(frozen=True, slots=True)
class Equipment:
    """Installed components, numbered as in the Fröling documentation (1-based)."""

    heating_circuits: tuple[int, ...]
    hot_water: tuple[int, ...]
    buffers: tuple[int, ...]
    circulation_pump: bool = False


@dataclass(frozen=True, slots=True)
class HeatingCircuitValues:
    """Current values of one heating circuit."""

    flow_temperature: float | None
    flow_setpoint: float | None
    room_temperature: float | None


@dataclass(frozen=True, slots=True)
class HotWaterValues:
    """Current values of one hot water tank."""

    temperature: float | None
    pump: float | None


@dataclass(frozen=True, slots=True)
class BufferValues:
    """Current values of one buffer tank."""

    top: float | None
    middle: float | None
    bottom: float | None
    pump: float | None


@dataclass(frozen=True, slots=True)
class CirculationValues:
    """Current values of the circulation pump."""

    flow_switch: bool | None
    pump: float | None
    # None if no return sensor is configured (40603 = 0).
    return_temperature: float | None


@dataclass(frozen=True, slots=True)
class LambdatronicValues:
    """Current values of the whole system."""

    boiler_temperature: float | None
    flue_gas_temperature: float | None
    residual_oxygen: float | None
    induced_draft_speed: float | None
    return_temperature: float | None
    boiler_control_output: float | None
    operating_hours: float | None
    hours_since_maintenance: float | None
    pellet_hopper_level: float | None
    burner_starts: float | None
    feed_rate: float | None
    boiler_setpoint_calculated: float | None
    heating_hours: float | None
    # Resettable pellet counter in kg (1000 · t part + kg part).
    pellet_counter: float | None
    pellet_consumption_total: float | None
    hours_until_ash_emptying: float | None
    outside_temperature: float | None
    boiler_demand: bool | None
    system_state: int | None
    boiler_state: int | None
    errors: tuple[int, ...]
    heating_circuits: Mapping[int, HeatingCircuitValues]
    hot_water: Mapping[int, HotWaterValues]
    buffers: Mapping[int, BufferValues]
    circulation: CirculationValues | None
    raw: Mapping[int, int]

    def flat(self) -> dict[str, FlatValue]:
        """All values under full keys ('boiler_temperature', 'hc1_flow_temperature', 'circulation_pump')."""
        return _flatten(self)

    @property
    def fault_active(self) -> bool:
        """True if the error buffer has entries or the boiler is in a fault state."""
        return bool(self.errors) or (
            self.boiler_state is not None and self.boiler_state in FAULT_BOILER_STATES
        )


@dataclass(frozen=True, slots=True)
class HeatingCircuitParameters:
    """Settings of one heating circuit."""

    curve_flow_plus10: float | None
    curve_flow_minus10: float | None
    setback: float | None
    max_flow: float | None
    heating_limit: float | None
    heating_limit_setback: float | None
    operating_mode: int | None
    room_setpoint_heating: float | None
    room_setpoint_setback: float | None
    frost_protection: float | None
    pump_off_below_flow_setpoint: float | None
    buffer_overheat_protection: float | None

    def flow_for_outside(self, outside: float) -> float | None:
        """Flow temperature of the heating curve: a line through (+10 °C, VL+10) and (-10 °C, VL-10)."""
        if self.curve_flow_plus10 is None or self.curve_flow_minus10 is None:
            return None
        flow = (
            self.curve_flow_plus10 + (self.curve_flow_minus10 - self.curve_flow_plus10) * (10 - outside) / 20
        )
        if self.max_flow is not None:
            flow = min(flow, self.max_flow)
        return round(flow, 1)

    def curve(self, start: int = -20, stop: int = 20, step: int = 5) -> list[tuple[int, float]]:
        """Points (outside temperature, flow temperature) of the heating curve."""
        points: list[tuple[int, float]] = []
        for outside in range(start, stop + 1, step):
            flow = self.flow_for_outside(outside)
            if flow is None:
                return []
            points.append((outside, flow))
        return points


@dataclass(frozen=True, slots=True)
class HotWaterParameters:
    """Settings of one hot water tank."""

    setpoint: float | None
    recharge_below: float | None


@dataclass(frozen=True, slots=True)
class BufferParameters:
    """Settings of one buffer tank."""

    circuit_release: float | None
    # Boiler setpoint minus the boiler start difference of this buffer (read-only, derived).
    ignition_below_buffer_temperature: float | None


@dataclass(frozen=True, slots=True)
class LambdatronicParameters:
    """Settings of the system (mostly writable)."""

    boiler_setpoint: float | None
    pellet_stock: float | None
    pellet_stock_minimum: float | None
    pellet_filling_start_1: time | None
    pellet_filling_start_2: time | None
    auto_feeder_off: bool | None
    circulation_pump_off_above: float | None
    heating_circuits: Mapping[int, HeatingCircuitParameters]
    hot_water: Mapping[int, HotWaterParameters]
    buffers: Mapping[int, BufferParameters]
    raw: Mapping[int, int]

    def flat(self) -> dict[str, FlatValue]:
        """All settings under full keys ('boiler_setpoint', 'hc1_operating_mode', 'dhw1_setpoint')."""
        return _flatten(self)
