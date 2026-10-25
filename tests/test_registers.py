"""Tests for register definitions and block planning."""

import math

import pytest

from froeling_lambdatronic.exceptions import ReadOnlyRegisterError, ValueOutOfRangeError
from froeling_lambdatronic.registers import (
    BOILER,
    ERROR_SLOTS,
    MAX_HEATING_CIRCUITS,
    ReadBlock,
    RegisterDef,
    Space,
    buffer,
    heating_circuit,
    hot_water,
    plan_blocks,
)


def test_space_and_address() -> None:
    assert BOILER.boiler_temperature.space is Space.INPUT
    assert BOILER.boiler_temperature.address == 0
    assert BOILER.boiler_setpoint.space is Space.HOLDING
    assert BOILER.boiler_setpoint.address == 0
    assert heating_circuit(2).operating_mode.address == 8047


def test_invalid_number_rejected() -> None:
    with pytest.raises(ValueError, match="register number"):
        RegisterDef(key="x", number=20001)


@pytest.mark.parametrize(
    ("definition", "raw", "expected"),
    [
        (BOILER.boiler_temperature, 134, 67.0),
        (BOILER.residual_oxygen, 91, 9.1),
        (BOILER.outside_temperature, 0xFFF6, -5.0),
        (BOILER.operating_hours, 40000, 40000.0),
        (BOILER.pellet_hopper_level, 20700, 100.0),
        (BOILER.pellet_consumption_total, 429, 42.9),
        (BOILER.boiler_temperature, 0xFFFF, None),
        (heating_circuit(1).room_temperature, 254, None),
        (BOILER.boiler_temperature, 254, 127.0),
    ],
)
def test_decode(definition: RegisterDef, raw: int, expected: float | None) -> None:
    value = definition.decode(raw)
    if expected is None:
        assert value is None
    else:
        assert value == pytest.approx(expected, abs=0.05)


def test_encode_rounds_to_step_and_scales() -> None:
    assert heating_circuit(1).curve_flow_plus10.encode(46.2) == 92
    assert heating_circuit(1).curve_flow_plus10.encode(46.3) == 93
    assert BOILER.pellet_stock.encode(4.5) == 45
    assert heating_circuit(1).operating_mode.encode(1) == 1


def test_encode_negative_value_is_twos_complement() -> None:
    definition = RegisterDef(key="t", number=40999, divisor=2, writable=True, minimum=-20, maximum=20)
    assert definition.encode(-5) == 0x10000 - 10


def test_encode_checks_range_and_writability() -> None:
    with pytest.raises(ValueOutOfRangeError) as err:
        BOILER.boiler_setpoint.encode(95)
    assert (err.value.minimum, err.value.maximum, err.value.value) == (70, 90, 95)
    with pytest.raises(ReadOnlyRegisterError):
        BOILER.boiler_temperature.encode(60)


def test_factories_offsets_and_bounds() -> None:
    hc2 = heating_circuit(2)
    assert hc2.flow_temperature.number == 31061
    assert hc2.curve_flow_plus10.number == 41062
    assert hc2.heating_limit_setback.number == 41068
    assert hc2.curve_flow_plus10.key == "hc2_curve_flow_plus10"
    assert hot_water(1).temperature.number == 31631
    assert hot_water(2).setpoint.number == 41662
    assert buffer(2).top.number == 32041
    for factory, limit in ((heating_circuit, MAX_HEATING_CIRCUITS), (hot_water, 8), (buffer, 4)):
        with pytest.raises(ValueError, match="out of range"):
            factory(0)
        with pytest.raises(ValueError, match="out of range"):
            factory(limit + 1)


def test_error_slots() -> None:
    assert len(ERROR_SLOTS) == 20
    assert ERROR_SLOTS[0].number == 33001
    assert ERROR_SLOTS[-1].number == 33020


def test_plan_blocks_merges_nearby_and_splits_spaces() -> None:
    blocks = plan_blocks(
        [
            BOILER.boiler_temperature,
            BOILER.hours_until_ash_emptying,
            BOILER.system_state,
            BOILER.boiler_setpoint,
        ]
    )
    assert blocks == [
        ReadBlock(Space.INPUT, 0, 1),
        ReadBlock(Space.INPUT, 86, 1),
        ReadBlock(Space.INPUT, 4000, 1),
        ReadBlock(Space.HOLDING, 0, 1),
    ]
    merged = plan_blocks(
        [
            BOILER.boiler_temperature,
            BOILER.flue_gas_temperature,
            BOILER.residual_oxygen,
            BOILER.induced_draft_speed,
        ]
    )
    assert merged == [ReadBlock(Space.INPUT, 0, 7)]
    assert plan_blocks([BOILER.boiler_temperature, BOILER.boiler_output]) == [
        ReadBlock(Space.INPUT, 0, 1),
        ReadBlock(Space.INPUT, 14, 1),
    ]


def test_plan_blocks_respects_max_count() -> None:
    definitions = [RegisterDef(key=f"r{i}", number=30001 + i) for i in range(0, 130)]
    blocks = plan_blocks(definitions)
    assert [b.count for b in blocks] == [122, 8]
    assert plan_blocks([]) == []


def test_read_block_lookup() -> None:
    block = ReadBlock(Space.INPUT, 0, 5)
    assert block.contains(BOILER.residual_oxygen)
    assert not block.contains(BOILER.boiler_setpoint)
    assert block.raw([10, 11, 12, 13, 14], BOILER.residual_oxygen) == 13


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_encode_rejects_non_finite_values(value: float) -> None:
    with pytest.raises(ValueOutOfRangeError):
        BOILER.boiler_setpoint.encode(value)
    unbounded = RegisterDef(key="t", number=40999, writable=True)
    with pytest.raises(ValueOutOfRangeError):
        unbounded.encode(value)
