"""Tests for Fröling texts."""

from froeling_lambdatronic.texts import (
    BOILER_STATES,
    FAULT_BOILER_STATES,
    HEATING_CIRCUIT_MODES,
    SYSTEM_STATES,
    boiler_state_text,
    error_text,
    normalize_heating_circuit_mode,
    system_state_text,
)


def test_state_tables_complete() -> None:
    assert set(SYSTEM_STATES) == set(range(9))
    assert set(BOILER_STATES) == set(range(83))
    assert SYSTEM_STATES[2] == "Automatik"
    assert BOILER_STATES[19] == "Betriebsbereit"
    assert BOILER_STATES[82] == "Sensorcheck"


def test_fault_states() -> None:
    assert {0, 41, 54, 62, 63, 74, 81} <= FAULT_BOILER_STATES
    assert 3 not in FAULT_BOILER_STATES
    assert all(BOILER_STATES[c].startswith(("STÖRUNG", "FEHLER", "SH Störung")) for c in FAULT_BOILER_STATES)


def test_unknown_codes() -> None:
    assert system_state_text(99) == "Unbekannt (99)"
    assert boiler_state_text(300) == "Unbekannt (300)"
    assert boiler_state_text(3) == "Heizen"


def test_heating_circuit_modes() -> None:
    assert HEATING_CIRCUIT_MODES[4] == "Dauerabsenken"
    assert normalize_heating_circuit_mode(1) == 1
    assert normalize_heating_circuit_mode(6) == 5
    assert normalize_heating_circuit_mode(11) == 0
    assert normalize_heating_circuit_mode(12) == 4
    assert normalize_heating_circuit_mode(42) is None


def test_error_texts() -> None:
    assert error_text(11) == "Zündversuch ist nicht gelungen"
    assert error_text(429).startswith("Vorlauffühler")
    assert error_text(62) == "Fehler 62"
    assert error_text(9999) == "Fehler 9999"
