"""Fröling texts for states, operating modes and errors (German, document B1200522)."""

import json
from collections.abc import Mapping
from functools import cache
from importlib.resources import files
from types import MappingProxyType

SYSTEM_STATES: Mapping[int, str] = MappingProxyType(
    {
        0: "Dauerlast",
        1: "Brauchwasser",
        2: "Automatik",
        3: "Scheitholzbetrieb",
        4: "Reinigen",
        5: "Ausgeschaltet",
        6: "Extraheizen",
        7: "Kaminkehrer",
        8: "Reinigen",
    }
)

BOILER_STATES: Mapping[int, str] = MappingProxyType(
    dict(
        enumerate(
            [
                "STÖRUNG",
                "Kessel Aus",
                "Anheizen",
                "Heizen",
                "Feuererhaltung",
                "Feuer Aus",
                "Tür offen",
                "Vorbereitung",
                "Vorwärmen",
                "Zünden",
                "Abstellen Warten",
                "Abstellen Warten1",
                "Abstellen Einschub1",
                "Abstellen Warten2",
                "Abstellen Einschub2",
                "Abreinigen",
                "2h warten",
                "Saugen/Heizen",
                "Fehlzündung",
                "Betriebsbereit",
                "Rost schließen",
                "Stoker leeren",
                "Vorheizen",
                "Saugen",
                "RSE schließen",
                "RSE öffnen",
                "Rost kippen",
                "Vorwärmen-Zünden",
                "Resteinschub",
                "Stoker auffüllen",
                "Lambdasonde aufheizen",
                "Gebläsenachlauf I",
                "Gebläsenachlauf II",
                "Abgestellt",
                "Nachzünden",
                "Zünden Warten",
                "FB: RSE schließen",
                "FB: Kessel belüften",
                "FB: Zünden",
                "FB: min. Einschub",
                "RSE schließen",
                "STÖRUNG: STB/NA",
                "STÖRUNG: Kipprost",
                "STÖRUNG: FR-Überdr.",
                "STÖRUNG: Türkont.",
                "STÖRUNG: Saugzug",
                "STÖRUNG: Umfeld",
                "FEHLER: STB/NA",
                "FEHLER: Kipprost",
                "FEHLER: FR-Überdr.",
                "FEHLER: Türkont.",
                "FEHLER: Saugzug",
                "FEHLER: Umfeld",
                "FEHLER: Stoker",
                "STÖRUNG: Stoker",
                "FB: Stoker leeren",
                "Vorbelüften",
                "STÖRUNG: Hackgut",
                "FEHLER: Hackgut",
                "NB: Tür offen",
                "NB: Anheizen",
                "NB: Heizen",
                "FEHLER: STB/NA",
                "FEHLER: Allgemein",
                "NB: Feuer Aus",
                "Selbsttest aktiv",
                "Fehlerbeh. 20min",
                "FEHLER: Fallschacht",
                "STÖRUNG: Fallschacht",
                "Reinigen möglich",
                "Heizen - Reinigen",
                "SH Anheizen",
                "SH Heizen",
                "SH Heiz/Abstell",
                "STÖRUNG sicher",
                "AGR Nachlauf",
                "AGR reinigen",
                "Zündung AUS",
                "Filter reinigen",
                "Anheizassistent",
                "SH Zünden",
                "SH Störung",
                "Sensorcheck",
            ]
        )
    )
)

FAULT_BOILER_STATES: frozenset[int] = frozenset(
    code for code, text in BOILER_STATES.items() if text.startswith(("STÖRUNG", "FEHLER")) or code == 81
)

HEATING_CIRCUIT_MODES: Mapping[int, str] = MappingProxyType(
    {0: "Aus", 1: "Automatik", 2: "Extraheizen", 3: "Absenken", 4: "Dauerabsenken", 5: "Partybetrieb"}
)

# Values the boiler reports in 48047+ that do not match the documented write values.
_MODE_ALIASES: Mapping[int, int] = MappingProxyType({6: 5, 11: 0, 12: 4})


def normalize_heating_circuit_mode(raw: int) -> int | None:
    """Map a read heating circuit mode to 0..5, None if unknown."""
    value = _MODE_ALIASES.get(raw, raw)
    return value if value in HEATING_CIRCUIT_MODES else None


@cache
def _error_texts() -> Mapping[int, str]:
    raw: dict[str, str] = json.loads(
        files("froeling_lambdatronic").joinpath("data/error_texts.json").read_text(encoding="utf-8")
    )
    return MappingProxyType({int(code): text for code, text in raw.items()})


def error_text(code: int) -> str:
    """Text of a Fröling error code."""
    return _error_texts().get(code, f"Fehler {code}")


def system_state_text(code: int) -> str:
    """Text of the system state (register 34001)."""
    return SYSTEM_STATES.get(code, f"Unbekannt ({code})")


def boiler_state_text(code: int) -> str:
    """Text of the boiler state (register 34002)."""
    return BOILER_STATES.get(code, f"Unbekannt ({code})")
