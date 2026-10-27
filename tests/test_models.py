"""Tests for data models."""

import dataclasses
from datetime import time

import pytest

from froeling_lambdatronic.models import (
    BufferParameters,
    BufferValues,
    CirculationValues,
    Equipment,
    HeatingCircuitParameters,
    HeatingCircuitValues,
    HotWaterParameters,
    HotWaterValues,
    LambdatronicParameters,
    LambdatronicValues,
    minutes_to_time,
    time_to_minutes,
)
from froeling_lambdatronic.registers import register_for_key


def params(**overrides: float | int | None) -> HeatingCircuitParameters:
    base: dict[str, float | int | None] = {
        "curve_flow_plus10": 46.0,
        "curve_flow_minus10": 63.0,
        "setback": 15.0,
        "max_flow": 75.0,
        "heating_limit": 20.0,
        "heating_limit_setback": 7.0,
        "operating_mode": 1,
        "room_setpoint_heating": 20.0,
        "room_setpoint_setback": 16.0,
        "frost_protection": 10.0,
        "pump_off_below_flow_setpoint": 20.0,
        "buffer_overheat_protection": 120.0,
    }
    return HeatingCircuitParameters(**(base | overrides))  # type: ignore[arg-type]


def test_flow_for_outside_is_linear_and_capped() -> None:
    p = params()
    assert p.flow_for_outside(10) == 46.0
    assert p.flow_for_outside(-10) == 63.0
    assert p.flow_for_outside(9.5) == pytest.approx(46.4, abs=0.05)
    assert p.flow_for_outside(-40) == 75.0
    assert params(max_flow=None).flow_for_outside(-40) == pytest.approx(88.5)
    assert params(curve_flow_plus10=None).flow_for_outside(0) is None


def test_curve_points() -> None:
    assert params().curve(-10, 10, 10) == [(-10, 63.0), (0, 54.5), (10, 46.0)]
    assert params(curve_flow_minus10=None).curve() == []


def _values(errors: tuple[int, ...], boiler_state: int | None) -> LambdatronicValues:
    fields: dict[str, object] = {f.name: None for f in dataclasses.fields(LambdatronicValues)}
    fields.update(
        {
            "errors": errors,
            "boiler_state": boiler_state,
            "heating_circuits": {},
            "hot_water": {},
            "buffers": {},
            "raw": {},
        }
    )
    return LambdatronicValues(**fields)  # type: ignore[arg-type]


def test_fault_active() -> None:
    assert not _values((), 3).fault_active
    assert _values((11,), 3).fault_active
    assert _values((), 41).fault_active
    assert not _values((), None).fault_active


def test_equipment_circulation_pump_defaults_to_false() -> None:
    assert Equipment((1,), (1,), (1,)).circulation_pump is False
    assert Equipment((1,), (), (), circulation_pump=True).circulation_pump is True


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [(0, time(0, 0)), (600, time(10, 0)), (1320, time(22, 0)), (1439, time(23, 59))],
)
def test_minutes_and_time_round_trip(minutes: int, expected: time) -> None:
    assert minutes_to_time(minutes) == expected
    assert time_to_minutes(expected) == minutes


@pytest.mark.parametrize("minutes", [-1, 1440, 2400])
def test_minutes_to_time_rejects_out_of_range(minutes: int) -> None:
    with pytest.raises(ValueError, match=r"0\.\.1439"):
        minutes_to_time(minutes)


def test_time_to_minutes_ignores_seconds() -> None:
    assert time_to_minutes(time(4, 30, 59)) == 270


def _full_values(circulation: CirculationValues | None) -> LambdatronicValues:
    return LambdatronicValues(
        boiler_temperature=60.0,
        flue_gas_temperature=53.0,
        residual_oxygen=2.0,
        induced_draft_speed=0.0,
        return_temperature=50.0,
        boiler_control_output=99.0,
        operating_hours=17716.0,
        hours_since_maintenance=17721.0,
        pellet_hopper_level=100.0,
        burner_starts=10486.0,
        feed_rate=0.0,
        boiler_setpoint_calculated=70.0,
        heating_hours=12856.0,
        pellet_counter=1130.0,
        pellet_consumption_total=42.9,
        hours_until_ash_emptying=221.0,
        outside_temperature=9.0,
        boiler_demand=False,
        system_state=2,
        boiler_state=19,
        errors=(11,),
        heating_circuits={1: HeatingCircuitValues(36.0, 0.0, None), 2: HeatingCircuitValues(29.0, 0.0, None)},
        hot_water={1: HotWaterValues(53.5, 0.0)},
        buffers={1: BufferValues(62.0, None, 55.5, 0.0)},
        circulation=circulation,
        raw={30001: 120},
    )


def test_values_flat_uses_full_keys() -> None:
    flat = _full_values(CirculationValues(False, 0.0, None)).flat()
    assert flat["boiler_temperature"] == 60.0
    assert flat["boiler_control_output"] == 99.0
    assert flat["pellet_counter"] == 1130.0
    assert flat["system_state"] == 2
    assert flat["boiler_demand"] is False
    assert flat["hc1_flow_temperature"] == 36.0
    assert flat["hc2_flow_temperature"] == 29.0
    assert flat["hc2_room_temperature"] is None
    assert flat["dhw1_temperature"] == 53.5
    assert flat["dhw1_pump"] == 0.0
    assert flat["buffer1_top"] == 62.0
    assert flat["buffer1_middle"] is None
    assert flat["circulation_flow_switch"] is False
    assert flat["circulation_pump"] == 0.0
    assert flat["circulation_return_temperature"] is None
    for excluded in ("errors", "raw", "fault_active", "heating_circuits", "circulation"):
        assert excluded not in flat
    assert len(flat) == 20 + 3 * 2 + 2 + 4 + 3


def test_values_flat_without_circulation() -> None:
    flat = _full_values(None).flat()
    assert not [key for key in flat if key.startswith("circulation")]


def _full_parameters() -> LambdatronicParameters:
    return LambdatronicParameters(
        boiler_setpoint=70.0,
        pellet_stock=4.5,
        pellet_stock_minimum=0.0,
        pellet_filling_start_1=time(10, 0),
        pellet_filling_start_2=time(22, 0),
        auto_feeder_off=False,
        circulation_pump_off_above=35.0,
        heating_circuits={1: params(), 2: params(curve_flow_plus10=29.0)},
        hot_water={1: HotWaterParameters(55.0, 45.0)},
        buffers={1: BufferParameters(30.0, 55.0)},
        raw={40001: 140},
    )


def test_parameters_flat_uses_full_keys() -> None:
    flat = _full_parameters().flat()
    assert flat["boiler_setpoint"] == 70.0
    assert flat["pellet_filling_start_1"] == time(10, 0)
    assert flat["pellet_filling_start_2"] == time(22, 0)
    assert flat["auto_feeder_off"] is False
    assert flat["circulation_pump_off_above"] == 35.0
    assert flat["hc1_operating_mode"] == 1
    assert flat["hc2_curve_flow_plus10"] == 29.0
    assert flat["hc1_buffer_overheat_protection"] == 120.0
    assert flat["dhw1_setpoint"] == 55.0
    assert flat["dhw1_recharge_below"] == 45.0
    assert flat["buffer1_circuit_release"] == 30.0
    assert flat["buffer1_ignition_below_buffer_temperature"] == 55.0
    assert "raw" not in flat
    assert len(flat) == 7 + 12 * 2 + 2 + 2


def test_parameter_keys_map_to_registers() -> None:
    derived = {"buffer1_ignition_below_buffer_temperature"}
    read_only = {"pellet_filling_start_2", "hc1_max_flow", "hc2_max_flow"}
    for key in _full_parameters().flat():
        definition = register_for_key(key)
        if key in derived:
            assert definition is None
            continue
        assert definition is not None, key
        assert definition.key == key
        assert definition.writable is (key not in read_only), key
