"""Declarative register catalogue of the Lambdatronic 3200 (Fröling document B1200522)."""

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cache

from .exceptions import ReadOnlyRegisterError, ValueOutOfRangeError

MAX_HEATING_CIRCUITS = 18
MAX_HOT_WATER = 8
MAX_BUFFERS = 4

_UNAVAILABLE = 0xFFFF
_PROBE_UNPLUGGED = 254


class Space(StrEnum):
    """Register space."""

    INPUT = "input"
    HOLDING = "holding"


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterDef:
    """One documented register (number as in the Fröling documentation, e.g. 30001)."""

    key: str
    number: int
    divisor: float = 1.0
    signed: bool = True
    unit: str | None = None
    writable: bool = False
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    probe: bool = False

    def __post_init__(self) -> None:
        if not 30001 <= self.number <= 49999:
            raise ValueError(f"register number {self.number} is not an input/holding register")

    @property
    def space(self) -> Space:
        """Input (FC04) or holding (FC03/FC06) register."""
        return Space.INPUT if self.number < 40001 else Space.HOLDING

    @property
    def address(self) -> int:
        """Zero-based protocol address."""
        return self.number - (30001 if self.space is Space.INPUT else 40001)

    def decode(self, raw: int) -> float | None:
        """Scale a raw register value; None for 'not available' or an unplugged probe."""
        if raw == _UNAVAILABLE or (self.probe and raw == _PROBE_UNPLUGGED):
            return None
        value = raw - 0x10000 if self.signed and raw >= 0x8000 else raw
        return value / self.divisor

    def encode(self, value: float) -> int:
        """Validate, round to the register step and convert to a raw 16-bit value."""
        if not self.writable:
            raise ReadOnlyRegisterError(self.key)
        if not math.isfinite(value):
            raise ValueOutOfRangeError(self.key, value, self.minimum, self.maximum)
        step = self.step if self.step is not None else 1 / self.divisor
        rounded = round(round(value / step) * step, 6)
        if (self.minimum is not None and rounded < self.minimum) or (
            self.maximum is not None and rounded > self.maximum
        ):
            raise ValueOutOfRangeError(self.key, value, self.minimum, self.maximum)
        raw = round(rounded * self.divisor)
        return raw + 0x10000 if raw < 0 else raw


def _temp(key: str, number: int, *, probe: bool = True) -> RegisterDef:
    return RegisterDef(key=key, number=number, divisor=2, unit="°C", probe=probe)


def _setting(key: str, number: int, minimum: float, maximum: float, unit: str = "°C") -> RegisterDef:
    return RegisterDef(
        key=key,
        number=number,
        divisor=2,
        unit=unit,
        writable=True,
        minimum=minimum,
        maximum=maximum,
        step=0.5,
    )


def _counter(key: str, number: int, unit: str | None = None, divisor: float = 1.0) -> RegisterDef:
    return RegisterDef(key=key, number=number, divisor=divisor, signed=False, unit=unit)


@dataclass(frozen=True, slots=True)
class BoilerRegisters:
    """Registers of the boiler itself and system-wide values."""

    boiler_temperature: RegisterDef = field(default=_temp("boiler_temperature", 30001, probe=False))
    flue_gas_temperature: RegisterDef = field(
        default=RegisterDef(key="flue_gas_temperature", number=30002, unit="°C")
    )
    residual_oxygen: RegisterDef = field(
        default=RegisterDef(key="residual_oxygen", number=30004, divisor=10, unit="%")
    )
    induced_draft_speed: RegisterDef = field(default=_counter("induced_draft_speed", 30007, "rpm"))
    return_temperature: RegisterDef = field(default=_temp("return_temperature", 30010))
    # Requested modulation of the output controller ("Kesselstellgröße"), not the delivered output.
    boiler_control_output: RegisterDef = field(
        default=RegisterDef(key="boiler_control_output", number=30015, unit="%")
    )
    operating_hours: RegisterDef = field(default=_counter("operating_hours", 30021, "h"))
    pellet_hopper_level: RegisterDef = field(
        default=RegisterDef(key="pellet_hopper_level", number=30022, divisor=207, signed=False, unit="%")
    )
    burner_starts: RegisterDef = field(default=_counter("burner_starts", 30023))
    feed_rate: RegisterDef = field(default=RegisterDef(key="feed_rate", number=30026, unit="%"))
    boiler_setpoint_calculated: RegisterDef = field(
        default=_temp("boiler_setpoint_calculated", 30028, probe=False)
    )
    hours_since_maintenance: RegisterDef = field(default=_counter("hours_since_maintenance", 30056, "h"))
    boiler_demand: RegisterDef = field(default=_counter("boiler_demand", 30057))
    heating_hours: RegisterDef = field(default=_counter("heating_hours", 30064, "h"))
    # The resettable pellet counter is split: 30082 holds the kg part (0..999), 30083 the tonnes.
    pellet_counter_kg: RegisterDef = field(default=_counter("pellet_counter_kg", 30082, "kg"))
    pellet_counter_t: RegisterDef = field(default=_counter("pellet_counter_t", 30083, "t"))
    pellet_consumption_total: RegisterDef = field(
        default=_counter("pellet_consumption_total", 30084, "t", divisor=10)
    )
    hours_until_ash_emptying: RegisterDef = field(default=_counter("hours_until_ash_emptying", 30087, "h"))
    outside_temperature: RegisterDef = field(default=_temp("outside_temperature", 31001))
    system_state: RegisterDef = field(default=_counter("system_state", 34001))
    boiler_state: RegisterDef = field(default=_counter("boiler_state", 34002))
    boiler_setpoint: RegisterDef = field(default=_setting("boiler_setpoint", 40001, 70, 90))
    # Filling start times are minutes after midnight (600 = 10:00); the documented maximum 2400 is unusable.
    pellet_filling_start_1: RegisterDef = field(
        default=RegisterDef(
            key="pellet_filling_start_1",
            number=40062,
            signed=False,
            unit="min",
            writable=True,
            minimum=0,
            maximum=1439,
            step=1,
        )
    )
    pellet_filling_start_2: RegisterDef = field(
        default=RegisterDef(key="pellet_filling_start_2", number=40095, signed=False, unit="min")
    )
    auto_feeder_off: RegisterDef = field(
        default=RegisterDef(
            key="auto_feeder_off", number=40265, signed=False, writable=True, minimum=0, maximum=1, step=1
        )
    )
    pellet_stock: RegisterDef = field(
        default=RegisterDef(
            key="pellet_stock",
            number=40320,
            divisor=10,
            signed=False,
            unit="t",
            writable=True,
            minimum=0,
            maximum=100,
            step=0.1,
        )
    )
    pellet_stock_minimum: RegisterDef = field(
        default=RegisterDef(
            key="pellet_stock_minimum",
            number=40336,
            divisor=10,
            signed=False,
            unit="t",
            writable=True,
            minimum=0,
            maximum=100,
            step=0.1,
        )
    )

    def all(self) -> tuple[RegisterDef, ...]:
        """All definitions in declaration order."""
        return tuple(getattr(self, name) for name in self.__dataclass_fields__)


BOILER = BoilerRegisters()

ERROR_SLOTS: tuple[RegisterDef, ...] = tuple(_counter(f"error_{i}", 33000 + i) for i in range(1, 21))


@dataclass(frozen=True, slots=True)
class HeatingCircuitRegisters:
    """Registers of heating circuit n."""

    number: int
    flow_temperature: RegisterDef
    flow_setpoint: RegisterDef
    room_temperature: RegisterDef
    curve_flow_plus10: RegisterDef
    curve_flow_minus10: RegisterDef
    setback: RegisterDef
    max_flow: RegisterDef
    heating_limit: RegisterDef
    heating_limit_setback: RegisterDef
    operating_mode: RegisterDef
    room_setpoint_heating: RegisterDef
    frost_protection: RegisterDef
    pump_off_below_flow_setpoint: RegisterDef
    room_setpoint_setback: RegisterDef
    buffer_overheat_protection: RegisterDef

    def all(self) -> tuple[RegisterDef, ...]:
        """All definitions of this circuit."""
        return tuple(getattr(self, name) for name in self.__dataclass_fields__ if name != "number")


@dataclass(frozen=True, slots=True)
class HotWaterRegisters:
    """Registers of hot water tank (Boiler) n."""

    number: int
    temperature: RegisterDef
    pump: RegisterDef
    setpoint: RegisterDef
    recharge_below: RegisterDef

    def all(self) -> tuple[RegisterDef, ...]:
        """All definitions of this tank."""
        return tuple(getattr(self, name) for name in self.__dataclass_fields__ if name != "number")


@dataclass(frozen=True, slots=True)
class BufferRegisters:
    """Registers of buffer tank n."""

    number: int
    top: RegisterDef
    middle: RegisterDef
    bottom: RegisterDef
    pump: RegisterDef
    circuit_release: RegisterDef
    ignition_difference: RegisterDef

    def all(self) -> tuple[RegisterDef, ...]:
        """All definitions of this buffer (current values and settings)."""
        return tuple(getattr(self, name) for name in self.__dataclass_fields__ if name != "number")


def _check(n: int, limit: int, what: str) -> None:
    if not 1 <= n <= limit:
        raise ValueError(f"{what} {n} out of range 1..{limit}")


# Irregular location of "Temp. am Puffer oben ab der der Überhitzungsschutz aktiv wird" (B1200522 S. 31/32):
# HK1 41048, HK2 41079, HK3..18 41046 + 30·(n-1).
_BUFFER_OVERHEAT_PROTECTION = {1: 41048, 2: 41079}


def heating_circuit(n: int) -> HeatingCircuitRegisters:
    """Registers of heating circuit n (1..18)."""
    _check(n, MAX_HEATING_CIRCUITS, "heating circuit")
    o = 30 * (n - 1)
    p = f"hc{n}_"
    return HeatingCircuitRegisters(
        number=n,
        flow_temperature=_temp(p + "flow_temperature", 31031 + o),
        flow_setpoint=_temp(p + "flow_setpoint", 31032 + o, probe=False),
        room_temperature=_temp(p + "room_temperature", 31033 + o),
        curve_flow_plus10=_setting(p + "curve_flow_plus10", 41032 + o, 10, 110),
        curve_flow_minus10=_setting(p + "curve_flow_minus10", 41033 + o, 10, 110),
        setback=_setting(p + "setback", 41034 + o, 0, 70, unit="K"),
        max_flow=_temp(p + "max_flow", 41035 + o, probe=False),
        # The PDF drops minus signs: printed "20..50" means -20..50 (confirmed by Fröling Connect).
        heating_limit=_setting(p + "heating_limit", 41037 + o, -20, 50),
        heating_limit_setback=_setting(p + "heating_limit_setback", 41038 + o, -20, 50),
        operating_mode=RegisterDef(
            key=p + "operating_mode",
            number=48047 + n - 1,
            signed=False,
            writable=True,
            minimum=0,
            maximum=5,
            step=1,
        ),
        room_setpoint_heating=_setting(p + "room_setpoint_heating", 41036 + o, 10, 30),
        frost_protection=_setting(p + "frost_protection", 41039 + o, -10, 20),
        pump_off_below_flow_setpoint=_setting(p + "pump_off_below_flow_setpoint", 41040 + o, 10, 30),
        room_setpoint_setback=_setting(p + "room_setpoint_setback", 41041 + o, 10, 30),
        buffer_overheat_protection=RegisterDef(
            key=p + "buffer_overheat_protection",
            number=_BUFFER_OVERHEAT_PROTECTION.get(n, 41046 + o),
            unit="°C",
            writable=True,
            minimum=60,
            maximum=120,
            step=1,
        ),
    )


def hot_water(n: int) -> HotWaterRegisters:
    """Registers of hot water tank n (1..8)."""
    _check(n, MAX_HOT_WATER, "hot water tank")
    o = 30 * (n - 1)
    p = f"dhw{n}_"
    return HotWaterRegisters(
        number=n,
        temperature=_temp(p + "temperature", 31631 + o),
        pump=RegisterDef(key=p + "pump", number=31633 + o, unit="%"),
        setpoint=_setting(p + "setpoint", 41632 + o, 10, 100),
        recharge_below=_setting(p + "recharge_below", 41633 + o, 1, 90),
    )


def buffer(n: int) -> BufferRegisters:
    """Registers of buffer tank n (1..4)."""
    _check(n, MAX_BUFFERS, "buffer")
    o = 40 * (n - 1)
    p = f"buffer{n}_"
    return BufferRegisters(
        number=n,
        top=_temp(p + "top", 32001 + o),
        middle=_temp(p + "middle", 32002 + o),
        bottom=_temp(p + "bottom", 32003 + o),
        pump=RegisterDef(key=p + "pump", number=32004 + o, unit="%"),
        circuit_release=_setting(p + "circuit_release", 42001 + o, 20, 100),
        # Boiler starts when the top buffer temperature drops this far below the boiler setpoint (read-only).
        ignition_difference=RegisterDef(key=p + "ignition_difference", number=42005 + o, divisor=2, unit="K"),
    )


@dataclass(frozen=True, slots=True)
class CirculationRegisters:
    """Registers of the circulation pump (B1200522 S. 13 and S. 29)."""

    flow_switch: RegisterDef = field(
        default=RegisterDef(key="circulation_flow_switch", number=30601, divisor=2)
    )
    pump: RegisterDef = field(default=RegisterDef(key="circulation_pump", number=30711, unit="%"))
    return_temperature: RegisterDef = field(default=_temp("circulation_return_temperature", 30712))
    pump_off_above: RegisterDef = field(default=_setting("circulation_pump_off_above", 40601, 20, 120))
    return_sensor_present: RegisterDef = field(default=_counter("circulation_return_sensor_present", 40603))

    def all(self) -> tuple[RegisterDef, ...]:
        """All definitions in declaration order."""
        return tuple(getattr(self, name) for name in self.__dataclass_fields__)


CIRCULATION = CirculationRegisters()


def register_for_key(key: str) -> RegisterDef | None:
    """Return the definition with this full key (e.g. 'hc2_curve_flow_plus10'), None if there is none.

    Derived values without a register of their own (e.g. 'pellet_counter') have no definition.
    """
    return _definitions_by_key().get(key)


@cache
def _definitions_by_key() -> dict[str, RegisterDef]:
    definitions = [*BOILER.all(), *ERROR_SLOTS, *CIRCULATION.all()]
    for n in range(1, MAX_HEATING_CIRCUITS + 1):
        definitions += heating_circuit(n).all()
    for n in range(1, MAX_HOT_WATER + 1):
        definitions += hot_water(n).all()
    for n in range(1, MAX_BUFFERS + 1):
        definitions += buffer(n).all()
    return {d.key: d for d in definitions}


@dataclass(frozen=True, slots=True)
class ReadBlock:
    """A contiguous range of registers read with one request."""

    space: Space
    address: int
    count: int

    def contains(self, definition: RegisterDef) -> bool:
        """Return True if the definition lies inside this block."""
        return (
            definition.space is self.space and self.address <= definition.address < self.address + self.count
        )

    def raw(self, values: list[int], definition: RegisterDef) -> int:
        """Pick the raw value of a definition from the values read for this block."""
        return values[definition.address - self.address]


def plan_blocks(
    definitions: Iterable[RegisterDef], max_gap: int = 10, max_count: int = 122
) -> list[ReadBlock]:
    """Group definitions into as few contiguous read blocks as sensible."""
    definitions = list(definitions)
    blocks: list[ReadBlock] = []
    for space in Space:
        addresses = sorted({d.address for d in definitions if d.space is space})
        if not addresses:
            continue
        start = end = addresses[0]
        for address in addresses[1:]:
            if address - end - 1 <= max_gap and address - start + 1 <= max_count:
                end = address
                continue
            blocks.append(ReadBlock(space, start, end - start + 1))
            start = end = address
        blocks.append(ReadBlock(space, start, end - start + 1))
    return blocks
