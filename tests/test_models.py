"""Tests for data models."""

import dataclasses

import pytest

from froeling_lambdatronic.models import HeatingCircuitParameters, LambdatronicValues


def params(**overrides: float | int | None) -> HeatingCircuitParameters:
    base: dict[str, float | int | None] = {
        "curve_flow_plus10": 46.0,
        "curve_flow_minus10": 63.0,
        "setback": 15.0,
        "max_flow": 75.0,
        "heating_limit": 20.0,
        "heating_limit_setback": 7.0,
        "operating_mode": 1,
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
