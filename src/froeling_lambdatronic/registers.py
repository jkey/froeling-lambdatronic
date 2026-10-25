"""Declarative register catalogue of the Lambdatronic 3200 (Fröling document B1200522)."""

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

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
    boiler_output: RegisterDef = field(default=RegisterDef(key="boiler_output", number=30015, unit="%"))
    operating_hours: RegisterDef = field(default=_counter("operating_hours", 30021, "h"))
    pellet_hopper_level: RegisterDef = field(
        default=RegisterDef(key="pellet_hopper_level", number=30022, divisor=207, signed=False, unit="%")
    )
    burner_starts: RegisterDef = field(default=_counter("burner_starts", 30023))
    feed_rate: RegisterDef = field(default=RegisterDef(key="feed_rate", number=30026, unit="%"))
    boiler_setpoint_calculated: RegisterDef = field(
        default=_temp("boiler_setpoint_calculated", 30028, probe=False)
    )
    boiler_demand: RegisterDef = field(default=_counter("boiler_demand", 30057))
    heating_hours: RegisterDef = field(default=_counter("heating_hours", 30064, "h"))
    pellet_counter_kg: RegisterDef = field(default=_counter("pellet_counter_kg", 30082, "kg"))
    pellet_consumption_total: RegisterDef = field(
        default=_counter("pellet_consumption_total", 30084, "t", divisor=10)
    )
    hours_until_ash_emptying: RegisterDef = field(default=_counter("hours_until_ash_emptying", 30087, "h"))
    outside_temperature: RegisterDef = field(default=_temp("outside_temperature", 31001))
    system_state: RegisterDef = field(default=_counter("system_state", 34001))
    boiler_state: RegisterDef = field(default=_counter("boiler_state", 34002))
    boiler_setpoint: RegisterDef = field(default=_setting("boiler_setpoint", 40001, 70, 90))
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

    def all(self) -> tuple[RegisterDef, ...]:
        """All definitions of this buffer."""
        return tuple(getattr(self, name) for name in self.__dataclass_fields__ if name != "number")


def _check(n: int, limit: int, what: str) -> None:
    if not 1 <= n <= limit:
        raise ValueError(f"{what} {n} out of range 1..{limit}")


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
        heating_limit=_setting(p + "heating_limit", 41037 + o, 0, 50),
        heating_limit_setback=_setting(p + "heating_limit_setback", 41038 + o, 0, 50),
        operating_mode=RegisterDef(
            key=p + "operating_mode",
            number=48047 + n - 1,
            signed=False,
            writable=True,
            minimum=0,
            maximum=5,
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
    )


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
