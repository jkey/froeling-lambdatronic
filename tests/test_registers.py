"""Tests for register definitions and block planning."""

import math

import pytest

from froeling_lambdatronic.exceptions import ReadOnlyRegisterError, ValueOutOfRangeError
from froeling_lambdatronic.registers import (
    BOILER,
    CIRCULATION,
    ERROR_SLOTS,
    MAX_BUFFERS,
    MAX_HEATING_CIRCUITS,
    MAX_HOT_WATER,
    ReadBlock,
    RegisterDef,
    Space,
    buffer,
    heating_circuit,
    hot_water,
    plan_blocks,
    register_for_key,
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
    assert plan_blocks([BOILER.boiler_temperature, BOILER.boiler_control_output]) == [
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


def _spec(definition: RegisterDef) -> tuple[object, ...]:
    return (
        definition.number,
        definition.divisor,
        definition.signed,
        definition.unit,
        definition.writable,
        definition.minimum,
        definition.maximum,
        definition.step,
    )


def test_boiler_control_output_replaces_boiler_output() -> None:
    # B1200522 p. 9, l. 416: 30015 "Kesselstellgröße" (control output), %, scale 1.
    assert BOILER.boiler_control_output.key == "boiler_control_output"
    assert _spec(BOILER.boiler_control_output) == (30015, 1, True, "%", False, None, None, None)
    assert not hasattr(BOILER, "boiler_output")


@pytest.mark.parametrize(
    ("definition", "key", "spec"),
    [
        # p. 10, l. 466: hours since last maintenance.
        (
            BOILER.hours_since_maintenance,
            "hours_since_maintenance",
            (30056, 1, False, "h", False, None, None, None),
        ),
        # p. 11, l. 501/502: resettable kg and t counters.
        (BOILER.pellet_counter_kg, "pellet_counter_kg", (30082, 1, False, "kg", False, None, None, None)),
        (BOILER.pellet_counter_t, "pellet_counter_t", (30083, 1, False, "t", False, None, None, None)),
        # p. 19, l. 913: start of 1st pellet filling, minutes after midnight (documented max 2400).
        (BOILER.pellet_filling_start_1, "pellet_filling_start_1", (40062, 1, False, "min", True, 0, 1439, 1)),
        # p. 20, l. 955: start of 2nd pellet filling, read-only via Modbus.
        (
            BOILER.pellet_filling_start_2,
            "pellet_filling_start_2",
            (40095, 1, False, "min", False, None, None, None),
        ),
        # p. 24, l. 1153: disable automatic pellet feeding, 0..1.
        (BOILER.auto_feeder_off, "auto_feeder_off", (40265, 1, False, None, True, 0, 1, 1)),
    ],
)
def test_new_boiler_registers(definition: RegisterDef, key: str, spec: tuple[object, ...]) -> None:
    assert definition.key == key
    assert _spec(definition) == spec


def test_new_heating_circuit_registers() -> None:
    hc1 = heating_circuit(1)
    # p. 31, l. 1537/1542/1543/1544: room setpoint heating, frost protection (printed 10..20 = -10..20),
    # pump off below flow setpoint, room setpoint setback.
    assert _spec(hc1.room_setpoint_heating) == (41036, 2, True, "°C", True, 10, 30, 0.5)
    assert _spec(hc1.frost_protection) == (41039, 2, True, "°C", True, -10, 20, 0.5)
    assert _spec(hc1.pump_off_below_flow_setpoint) == (41040, 2, True, "°C", True, 10, 30, 0.5)
    assert _spec(hc1.room_setpoint_setback) == (41041, 2, True, "°C", True, 10, 30, 0.5)
    hc2 = heating_circuit(2)
    assert hc2.room_setpoint_heating.number == 41066
    assert hc2.frost_protection.number == 41069
    assert hc2.pump_off_below_flow_setpoint.number == 41070
    assert hc2.room_setpoint_setback.number == 41071
    assert hc2.frost_protection.key == "hc2_frost_protection"


@pytest.mark.parametrize(
    ("n", "number"),
    [
        (1, 41048),  # p. 31, l. 1551
        (2, 41079),  # p. 32, l. 1578 (+31, irregular)
        (3, 41106),  # p. 32, l. 1595: from HK3 on 41046 + 30·(n-1)
        (18, 41556),
    ],
)
def test_buffer_overheat_protection_location(n: int, number: int) -> None:
    definition = heating_circuit(n).buffer_overheat_protection
    assert definition.key == f"hc{n}_buffer_overheat_protection"
    assert _spec(definition) == (number, 1, True, "°C", True, 60, 120, 1)


def test_heating_limits_allow_minus_20() -> None:
    # p. 31, l. 1538/1540: printed "20..50", the PDF drops the minus sign; the cloud reports -20..50.
    hc1 = heating_circuit(1)
    for definition in (hc1.heating_limit, hc1.heating_limit_setback):
        assert (definition.minimum, definition.maximum) == (-20, 50)
        assert definition.encode(-20) == 0x10000 - 40
        assert definition.decode(0x10000 - 40) == -20
        with pytest.raises(ValueOutOfRangeError):
            definition.encode(-20.5)


def test_new_buffer_registers() -> None:
    # p. 41, l. 2066/2070: circuit release (20..100, R/W) and boiler start difference (R).
    b1 = buffer(1)
    assert _spec(b1.circuit_release) == (42001, 2, True, "°C", True, 20, 100, 0.5)
    assert _spec(b1.ignition_difference) == (42005, 2, True, "K", False, None, None, None)
    b2 = buffer(2)
    assert b2.circuit_release.number == 42041
    assert b2.ignition_difference.number == 42045  # p. 41, l. 2117
    assert b2.circuit_release.key == "buffer2_circuit_release"
    assert buffer(4).ignition_difference.number == 42125
    assert b1.circuit_release in b1.all()
    assert b1.ignition_difference in b1.all()


def test_circulation_registers() -> None:
    # p. 13, l. 619/630/631 and p. 29, l. 1447/1450.
    assert _spec(CIRCULATION.flow_switch) == (30601, 2, True, None, False, None, None, None)
    assert _spec(CIRCULATION.pump) == (30711, 1, True, "%", False, None, None, None)
    assert _spec(CIRCULATION.return_temperature) == (30712, 2, True, "°C", False, None, None, None)
    assert CIRCULATION.return_temperature.probe
    assert _spec(CIRCULATION.pump_off_above) == (40601, 2, True, "°C", True, 20, 120, 0.5)
    assert _spec(CIRCULATION.return_sensor_present) == (40603, 1, False, None, False, None, None, None)
    assert [d.key for d in CIRCULATION.all()] == [
        "circulation_flow_switch",
        "circulation_pump",
        "circulation_return_temperature",
        "circulation_pump_off_above",
        "circulation_return_sensor_present",
    ]


@pytest.mark.parametrize(
    "key",
    [
        "boiler_setpoint",
        "boiler_control_output",
        "pellet_counter_kg",
        "pellet_filling_start_1",
        "pellet_filling_start_2",
        "auto_feeder_off",
        "hc1_operating_mode",
        "hc2_curve_flow_plus10",
        "hc18_buffer_overheat_protection",
        "dhw1_recharge_below",
        "dhw8_setpoint",
        "buffer1_circuit_release",
        "buffer4_ignition_difference",
        "circulation_pump_off_above",
        "circulation_return_temperature",
        "error_1",
    ],
)
def test_register_for_key(key: str) -> None:
    definition = register_for_key(key)
    assert definition is not None
    assert definition.key == key


@pytest.mark.parametrize(
    "key",
    [
        "pellet_counter",
        "ignition_below_buffer_temperature",
        "hc19_flow_temperature",
        "dhw0_setpoint",
        "x",
        "",
    ],
)
def test_register_for_key_unknown(key: str) -> None:
    assert register_for_key(key) is None


def test_register_keys_are_unique() -> None:
    definitions = [*BOILER.all(), *ERROR_SLOTS, *CIRCULATION.all()]
    for n in range(1, MAX_HEATING_CIRCUITS + 1):
        definitions += heating_circuit(n).all()
    for n in range(1, MAX_HOT_WATER + 1):
        definitions += hot_water(n).all()
    for n in range(1, MAX_BUFFERS + 1):
        definitions += buffer(n).all()
    assert len({d.key for d in definitions}) == len(definitions)
    assert len({d.number for d in definitions}) == len(definitions)
