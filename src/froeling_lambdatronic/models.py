"""Typed snapshots returned by LambdatronicClient."""

from collections.abc import Mapping
from dataclasses import dataclass

from .texts import FAULT_BOILER_STATES


@dataclass(frozen=True, slots=True)
class Equipment:
    """Installed components, numbered as in the Fröling documentation (1-based)."""

    heating_circuits: tuple[int, ...]
    hot_water: tuple[int, ...]
    buffers: tuple[int, ...]


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
class LambdatronicValues:
    """Current values of the whole system."""

    boiler_temperature: float | None
    flue_gas_temperature: float | None
    residual_oxygen: float | None
    induced_draft_speed: float | None
    return_temperature: float | None
    boiler_output: float | None
    operating_hours: float | None
    pellet_hopper_level: float | None
    burner_starts: float | None
    feed_rate: float | None
    boiler_setpoint_calculated: float | None
    heating_hours: float | None
    pellet_counter_kg: float | None
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
    raw: Mapping[int, int]

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
class LambdatronicParameters:
    """Writable settings of the system."""

    boiler_setpoint: float | None
    pellet_stock: float | None
    pellet_stock_minimum: float | None
    heating_circuits: Mapping[int, HeatingCircuitParameters]
    hot_water: Mapping[int, HotWaterParameters]
    raw: Mapping[int, int]
