"""Mapping of Fröling Connect parameters to the value keys of the Modbus models, and the cloud snapshot."""

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from typing import Literal

from .cloud_models import (
    CloudComponent,
    CloudComponentDetail,
    CloudOverview,
    ComponentType,
    Phase,
    Weekday,
)
from .models import Equipment, FlatValue

type CloudKind = Literal["number", "choice", "bool", "time"]


@dataclass(frozen=True, slots=True)
class CloudRef:
    """Where a value lives in Fröling Connect: component type and parameter name.

    `factor` converts the cloud unit to the library unit (e.g. t -> kg: 1000); `invert` flips a bool
    whose cloud value 0 means 'on'.
    """

    component: ComponentType
    name: str
    kind: CloudKind
    factor: float = 1.0
    invert: bool = False

    def decode(self, raw: str) -> FlatValue:
        """Convert a raw cloud string to the library value; None if it cannot be read."""
        if self.kind == "time":
            try:
                return time.fromisoformat(raw)
            except ValueError:
                return None
        number = _finite(raw)
        if number is None:
            return None
        if self.kind == "choice":
            return int(number)
        if self.kind == "bool":
            return (number != 0) != self.invert
        return round(number * self.factor, 6)


def _finite(raw: str) -> float | None:
    try:
        number = float(raw)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


_BOILER = ComponentType.BOILER
_CIRCUIT = ComponentType.CIRCUIT
_DHW = ComponentType.DHW
_BUFFER = ComponentType.BUFFER_TANK
_FEED = ComponentType.FEED_SYSTEM
_CIRCULATION = ComponentType.CIRCULATION_PUMP

# Key templates as in LambdatronicValues.flat() / LambdatronicParameters.flat(); "{n}" is the component
# number. Evidence: abgleich.md sections 1/2 (category A = identical) and 8 (B derived, C1 newly documented
# registers, C2/D cloud only). Not included: desiredRoomTemp (41036 or 41040, unresolved),
# bufferTankChargeDiskret (picture level), editable (meta flag), tonsCounter/kgCounter (see pellet_counter).
CLOUD_REFS: Mapping[str, CloudRef] = {
    # Boiler (component 1_100).
    "boiler_temperature": CloudRef(_BOILER, "boilerTemp", "number"),  # A: 30001
    "flue_gas_temperature": CloudRef(_BOILER, "flueGasTemp", "number"),  # A: 30002
    "residual_oxygen": CloudRef(_BOILER, "resOxygenContent", "number"),  # A: 30004
    "hours_until_ash_emptying": CloudRef(_BOILER, "hoursTillAshboxWarn", "number"),  # A: 30087
    "pellet_hopper_level": CloudRef(_BOILER, "pelletsFillLevel", "number"),  # A: 30022
    "boiler_state": CloudRef(_BOILER, "state", "choice"),  # A: 34002, same table 0..82
    "operating_hours": CloudRef(_BOILER, "operationHours", "number"),  # A: 30021
    "hours_since_maintenance": CloudRef(_BOILER, "hoursSinceLastMaintenance", "number"),  # C1: 30056
    "system_state": CloudRef(_BOILER, "mode2", "choice"),  # B: 34001 values 0..2, writable only in the cloud
    "boiler_setpoint": CloudRef(_BOILER, "boilerSetTemp", "number"),  # A: 40001 (cloud range 40..90)
    # B: CAL_B_1 = 40001 - 42005 (boiler start difference of buffer 1), reported on the boiler.
    "buffer{n}_ignition_below_buffer_temperature": CloudRef(_BOILER, "ignitionWhenBufferTempBelow", "number"),
    "boiler_on": CloudRef(_BOILER, "boilerOn", "bool", invert=True),  # D: 0 = on, 1 = off
    "remote_control_allowed": CloudRef(_BOILER, "remoteOn", "bool"),  # D
    "ignition_configured": CloudRef(_BOILER, "ignitionConfigured", "bool"),  # D
    "induced_draft_control": CloudRef(_BOILER, "fanControl", "number"),  # C2: 30013/30076/30105 unresolved
    "reset_ash_counter": CloudRef(_BOILER, "resetHoursTillAshboxWarn", "bool"),  # D: action, write True
    # Heating circuits (componentNumber = Modbus number, verified by the heating curves).
    "outside_temperature": CloudRef(_CIRCUIT, "outAirTemp", "number"),  # A: 31001, system-wide
    "hc{n}_flow_temperature": CloudRef(_CIRCUIT, "actualFlowTemp", "number"),  # A: 31031
    "hc{n}_flow_setpoint": CloudRef(_CIRCUIT, "setFlowTemp", "number"),  # A: 31032
    "hc{n}_operating_mode": CloudRef(_CIRCUIT, "mode", "choice"),  # A: 48047, same table 0..5
    "hc{n}_curve_flow_plus10": CloudRef(_CIRCUIT, "flowTempAtPlus10", "number"),  # A: 41032
    "hc{n}_curve_flow_minus10": CloudRef(_CIRCUIT, "flowTempAtMinus10", "number"),  # A: 41033
    "hc{n}_setback": CloudRef(_CIRCUIT, "setbackReductionFlowTemp", "number"),  # A: 41034
    "hc{n}_heating_limit": CloudRef(_CIRCUIT, "circuitPumpOnOutAirTempLimitHeating", "number"),  # A: 41037
    "hc{n}_heating_limit_setback": CloudRef(_CIRCUIT, "circuitPumpOnOutAirTempLimitSetback", "number"),  # A
    "hc{n}_frost_protection": CloudRef(_CIRCUIT, "frostProtectionTemp", "number"),  # C1: 41039
    "hc{n}_buffer_overheat_protection": CloudRef(_CIRCUIT, "bufferOverheatingProtection", "number"),  # C1
    "hc{n}_pump": CloudRef(_CIRCUIT, "circuitPumpControl", "bool"),  # C2: coil 1000 + 30·n unresolved
    # Hot water tanks.
    "dhw{n}_temperature": CloudRef(_DHW, "dhwTempTop", "number"),  # A: 31631
    "dhw{n}_pump": CloudRef(_DHW, "dhwPumpControl", "number"),  # A: 31633
    "dhw{n}_setpoint": CloudRef(_DHW, "setDhwTemp", "number"),  # A: 41632
    "dhw{n}_recharge_below": CloudRef(_DHW, "reloadBelowTemp", "number"),  # A: 41633
    "dhw{n}_mode": CloudRef(_DHW, "mode", "choice"),  # D: 1 auto, 2 extra charge (41631 refuted)
    # Buffer tanks.
    "buffer{n}_top": CloudRef(_BUFFER, "bufferTempTop", "number"),  # A: 32001
    "buffer{n}_bottom": CloudRef(_BUFFER, "bufferTempBottom", "number"),  # A: 32003
    "buffer{n}_pump": CloudRef(_BUFFER, "bufferPumpControl", "number"),  # A: 32004
    "buffer{n}_charge": CloudRef(_BUFFER, "bufferTankCharge", "number"),  # C2: 32007 read 0
    "buffer{n}_circuit_release": CloudRef(_BUFFER, "circuitReleaseTempLimit", "number"),  # C1: 42001
    # Feed system.
    "pellet_counter": CloudRef(_FEED, "pelletsUsageCounter", "number", factor=1000),  # B: t -> kg
    "pellet_stock": CloudRef(_FEED, "remainingPelletsAmount", "number"),  # A: 40320
    "pellet_stock_minimum": CloudRef(_FEED, "minLevel", "number"),  # A: 40336
    "pellet_consumption_total": CloudRef(_FEED, "totalPelletConsumption", "number"),  # A: 30084
    "pellet_filling_start_1": CloudRef(_FEED, "startFirstFilling", "time"),  # C1: 40062
    "pellet_filling_start_2": CloudRef(_FEED, "startSecondFilling", "time"),  # C1: 40095, Modbus read-only
    "auto_feeder_off": CloudRef(_FEED, "autoFeederOff", "bool"),  # C1: 40265
    "reset_pellet_counter": CloudRef(_FEED, "resetCounter", "bool"),  # D: action, write True
    # Circulation pump.
    "circulation_flow_switch": CloudRef(_CIRCULATION, "dhwFlowSwitch", "bool"),  # C1: 30601
    "circulation_pump": CloudRef(_CIRCULATION, "pumpControl", "number"),  # C1: 30711
    "circulation_return_temperature": CloudRef(_CIRCULATION, "returnTemp", "number"),  # C1: 30712
    "circulation_pump_off_above": CloudRef(_CIRCULATION, "pumpOffAboveTemp", "number"),  # C1: 40601
}

_PATTERNS = tuple(
    (re.compile(re.escape(template).replace(r"\{n\}", r"([1-9][0-9]*)")), template) for template in CLOUD_REFS
)


def cloud_ref(key: str) -> tuple[CloudRef, int] | None:
    """Resolve a full key such as 'hc2_flow_temperature' to its reference and component number.

    Keys without a number (e.g. 'boiler_temperature') refer to component number 1.
    """
    for pattern, template in _PATTERNS:
        match = pattern.fullmatch(key)
        if match:
            return CLOUD_REFS[template], int(match.group(1)) if match.groups() else 1
    return None


@dataclass(frozen=True, slots=True)
class CloudSnapshot:
    """Values read from Fröling Connect under the same full keys as the Modbus models."""

    components: tuple[CloudComponent, ...]
    values: Mapping[str, FlatValue]
    schedules: Mapping[str, Mapping[Weekday, tuple[Phase, ...]]]
    details_loaded: bool

    def value(self, key: str) -> FlatValue:
        """Converted value of a key; None if Fröling Connect did not report it."""
        return self.values.get(key)


def build_snapshot(overview: CloudOverview, details: Sequence[CloudComponentDetail] = ()) -> CloudSnapshot:
    """Combine the overview and optional component details; details win for the same key."""
    components: dict[str, CloudComponent] = {}
    values: dict[str, FlatValue] = {}
    schedules: dict[str, Mapping[Weekday, tuple[Phase, ...]]] = {}
    if overview.outside_temperature is not None:
        values["outside_temperature"] = CLOUD_REFS["outside_temperature"].decode(overview.outside_temperature)
    sources = [(c.component, c.values, c.schedule) for c in overview.components]
    sources += [
        (d.component, {name: p.value for name, p in d.parameters.items()}, d.schedule) for d in details
    ]
    for component, raw_values, schedule in sources:
        components.setdefault(component.component_id, component)
        if component.key is None:
            continue
        values.update(_values(component, raw_values))
        if schedule:
            schedules[component.key] = schedule
    return CloudSnapshot(tuple(components.values()), values, schedules, details_loaded=bool(details))


def _values(component: CloudComponent, raw_values: Mapping[str, str]) -> dict[str, FlatValue]:
    return {
        template.format(n=component.number): ref.decode(raw_values[ref.name])
        for template, ref in CLOUD_REFS.items()
        if ref.component is component.type and ref.name in raw_values
    }


def equipment_from_components(components: Iterable[CloudComponent]) -> Equipment:
    """Derive the installed equipment from the cloud component list."""
    components = list(components)

    def numbers(kind: ComponentType) -> tuple[int, ...]:
        return tuple(sorted(c.number for c in components if c.type is kind))

    return Equipment(
        heating_circuits=numbers(_CIRCUIT),
        hot_water=numbers(_DHW),
        buffers=numbers(_BUFFER),
        circulation_pump=any(c.type is _CIRCULATION for c in components),
    )


def merge_equipment(modbus: Equipment, cloud: Equipment) -> tuple[Equipment, tuple[str, ...]]:
    """Union of both sources and the component keys (e.g. 'hc3') found in only one of them."""
    deviations: list[str] = []
    merged: dict[str, tuple[int, ...]] = {}
    for field, prefix in (("heating_circuits", "hc"), ("hot_water", "dhw"), ("buffers", "buffer")):
        left, right = set(getattr(modbus, field)), set(getattr(cloud, field))
        merged[field] = tuple(sorted(left | right))
        deviations += [f"{prefix}{n}" for n in sorted(left ^ right)]
    if modbus.circulation_pump != cloud.circulation_pump:
        deviations.append("circulation")
    equipment = Equipment(
        heating_circuits=merged["heating_circuits"],
        hot_water=merged["hot_water"],
        buffers=merged["buffers"],
        circulation_pump=modbus.circulation_pump or cloud.circulation_pump,
    )
    return equipment, tuple(deviations)
