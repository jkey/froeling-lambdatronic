"""Tests for the Fröling Connect data models and parsers (anonymised real responses)."""

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from froeling_lambdatronic.cloud_models import (
    CloudComponent,
    CloudError,
    CloudHeader,
    ComponentType,
    Phase,
    Weekday,
    parse_component_detail,
    parse_component_list,
    parse_errors,
    parse_header,
    parse_overview,
    parse_system_info,
    validate_day,
)
from froeling_lambdatronic.exceptions import CommunicationError

CLOUD = Path(__file__).parent / "fixtures" / "cloud"

# sha256 of personal values from the original probes (street, e-mail, pairing code, equipment number,
# surname, user id, facility id). They must never appear in a fixture.
FORBIDDEN = {
    "aacb1816deabf51c122dcc51b60b306b591b9680de12d579367803e46804709d",  # street
    "fa846362d045c921a881f0ce00f5f5ac37c2e9888286277c7239d0e3e42bcc3a",  # e-mail
    "fdab00ab9bee9faee45b958b8a49d8874e5ea4d00a04ee3c240908624a45fce7",  # pairing code
    "416c90bb10763019d8e3a7d8b7fe0097afcb40f3b4097c7fbbf1b5f54fb803ed",  # equipment number
    "226e9acc607e557c132f4db5f2f560f1f3a61a3a18cdff3f085bafab53803bbb",  # surname
    "0b39d57e3c91ae6595bbd689435899ab5bc9b3becb62c5035340568bddaf7c10",  # user id
    "dfc567e55a012a5cec827558c8f56b6067c3af16d046302d1eeb3825be4a094c",  # facility id
}


def _phase(start_hour: int, start_minute: int, end_hour: int, end_minute: int) -> dict[str, int]:
    return {
        "startHour": start_hour,
        "startMinute": start_minute,
        "endHour": end_hour,
        "endMinute": end_minute,
    }


def load(name: str) -> Any:
    return json.loads((CLOUD / name).read_text())


def _tokens(data: Any) -> set[str]:
    if isinstance(data, dict):
        return set().union(*(_tokens(k) | _tokens(v) for k, v in data.items())) if data else set()
    if isinstance(data, list):
        return set().union(*(_tokens(item) for item in data)) if data else set()
    text = str(data)
    return {text, *re.split(r"[/?&=\s,]+", text)}


def test_fixtures_are_anonymised() -> None:
    files = sorted(CLOUD.glob("*.json"))
    assert len(files) == 14
    for path in files:
        hashes = {
            hashlib.sha256(token.encode()).hexdigest() for token in _tokens(json.loads(path.read_text()))
        }
        assert not hashes & FORBIDDEN, path.name
    # The tokenizer also splits URLs, so ids inside them are checked as well.
    assert {"user", "12345", "facility", "777"} <= _tokens(load("facilities.json"))


def test_parse_component_list() -> None:
    components = parse_component_list(load("component_list.json"))
    assert [c.key for c in components] == ["boiler", "hc1", "hc2", "dhw1", "buffer1", "feed", "circulation"]
    hc2 = components[2]
    assert hc2 == CloudComponent(
        component_id="300_3110",
        type=ComponentType.CIRCUIT,
        raw_type="CIRCUIT",
        number=2,
        standard_name="Heizkreis 02",
        display_name="Heizkreis 01",
    )


def test_unknown_component_type_is_kept() -> None:
    (component,) = parse_component_list(
        [{"componentId": "9_9", "componentNumber": 1, "type": "SOLAR", "displayName": "Solar"}]
    )
    assert component.type is None
    assert component.raw_type == "SOLAR"
    assert component.key is None
    assert component.standard_name == ""


@pytest.mark.parametrize(
    "payload",
    [
        {"componentId": "1_100"},
        ["1_100"],
        [{"componentNumber": 1, "type": "BOILER"}],
        [{"componentId": "1_100", "type": "BOILER"}],
        [{"componentId": "1_100", "componentNumber": "eins", "type": "BOILER"}],
        [{"componentId": "1_100", "componentNumber": 1, "type": 7}],
        [{"componentId": "1_100", "componentNumber": True, "type": "BOILER"}],
    ],
)
def test_malformed_component_list(payload: Any) -> None:
    with pytest.raises(CommunicationError):
        parse_component_list(payload)


def test_parse_component_detail_merges_views() -> None:
    detail = parse_component_detail(load("component_300_3100.json"))
    assert detail.component.key == "hc1"
    assert detail.component.standard_name == "Heizkreis 01"
    params = detail.parameters
    assert params["actualFlowTemp"].value == "37"
    assert params["mode"].options == {
        "0": "AUS",
        "1": "Auto",
        "2": "Extraheizen",
        "3": "Absenken",
        "4": "Dauerabsenken",
        "5": "Party",
    }
    limit = params["circuitPumpOnOutAirTempLimitHeating"]
    assert (limit.id, limit.minimum, limit.maximum, limit.editable) == ("7_80", -20.0, 50.0, True)
    assert limit.kind == "NumValueObject"
    assert limit.unit == "°C"
    assert limit.display_name.startswith("Außentemperatur")
    assert params["circuitPumpControl"].editable is False
    assert detail.schedule[Weekday.MONDAY] == (Phase(270, 1320),)
    assert detail.schedule[Weekday.SUNDAY] == (Phase(360, 1320),)
    assert detail.schedule_ids[Weekday.MONDAY] == 56
    assert len(detail.schedule_ids) == 7


def test_parse_component_detail_time_values_and_full_day() -> None:
    feed = parse_component_detail(load("component_1_906.json"))
    first = feed.parameters["startFirstFilling"]
    assert (first.value, first.kind, first.minimum, first.maximum) == ("10:00", "TimeValueObject", None, None)
    assert feed.parameters["autoFeederOff"].options == {"0": "NEIN", "1": "JA"}
    assert feed.schedule == {}
    buffer = parse_component_detail(load("component_400_4100.json"))
    assert buffer.schedule[Weekday.FRIDAY] == (Phase(0, 1440),)
    circulation = parse_component_detail(load("component_1_570.json"))
    assert circulation.schedule[Weekday.MONDAY] == (Phase(270, 540), Phase(660, 780), Phase(960, 1320))


def test_view_priority_setup_over_top_over_state() -> None:
    def param(value: str) -> dict[str, Any]:
        return {"id": "1", "name": "x", "value": value, "editable": True}

    raw = {
        "componentId": "1_100",
        "componentNumber": 1,
        "type": "BOILER",
        "stateView": [param("state"), {**param("only state"), "name": "y"}],
        "topView": {"configParams": {"x": param("top")}, "infoParams": [], "pictureUrl": "u"},
        "setupView": [param("setup")],
    }
    assert parse_component_detail(raw).parameters["x"].value == "setup"
    del raw["setupView"]
    parameters = parse_component_detail(raw).parameters
    assert parameters["x"].value == "top"
    assert parameters["y"].value == "only state"


def test_parameter_defaults() -> None:
    raw = {
        "componentId": "1_100",
        "componentNumber": 1,
        "type": "BOILER",
        "stateView": [{"id": "3_0", "name": "boilerTemp", "value": None, "minVal": "abc"}],
        "topView": None,
        "timeWindowsView": None,
    }
    param = parse_component_detail(raw).parameters["boilerTemp"]
    assert (param.value, param.unit, param.display_name, param.kind) == ("", "", "", "")
    assert (param.minimum, param.maximum, param.editable, param.options) == (None, None, False, {})


@pytest.mark.parametrize(
    "change",
    [
        {"stateView": {"name": "x"}},
        {"stateView": ["x"]},
        {"stateView": [{"id": "1"}]},
        {"stateView": [{"id": 1, "name": "x"}]},
        {"stateView": [{"id": "1", "name": "x", "stringListKeyValues": ["a"]}]},
        {"topView": ["x"]},
        {"topView": {"configParams": {"x": "1"}}},
        {"topView": {"configParams": "x"}},
        {"timeWindowsView": {"MONDAY": []}},
        {"timeWindowsView": ["x"]},
        {"timeWindowsView": [{"id": 1, "weekDay": "MONDAY", "phases": "x"}]},
        {"timeWindowsView": [{"id": "a", "weekDay": "MONDAY", "phases": []}]},
    ],
)
def test_malformed_component_detail(change: dict[str, Any]) -> None:
    raw = {"componentId": "1_100", "componentNumber": 1, "type": "BOILER"} | change
    with pytest.raises(CommunicationError):
        parse_component_detail(raw)
    with pytest.raises(CommunicationError):
        parse_component_detail(["not", "an", "object"])


def test_schedule_skips_empty_phases_and_unknown_days() -> None:
    raw = {
        "componentId": "300_3100",
        "componentNumber": 1,
        "type": "CIRCUIT",
        "timeWindowsView": [
            {"id": 1, "weekDay": "MONDAY", "phases": [_phase(6, 0, 6, 0), _phase(6, 0, 8, 0)]},
            {"id": 2, "weekDay": "HOLIDAY", "phases": [_phase(6, 0, 8, 0)]},
            {"id": 3, "weekDay": "TUESDAY", "phases": []},
        ],
    }
    detail = parse_component_detail(raw)
    assert detail.schedule == {Weekday.MONDAY: (Phase(360, 480),), Weekday.TUESDAY: ()}
    assert detail.schedule_ids == {Weekday.MONDAY: 1, Weekday.TUESDAY: 3}


@pytest.mark.parametrize(
    "broken",
    [
        "x",
        {"startHour": 1},
        _phase(22, 0, 6, 0),
        _phase(6, 0, 25, 0),
        {"startHour": True, "startMinute": 0, "endHour": 8, "endMinute": 0},
        {"startHour": 6, "startMinute": 0, "endHour": 8.5, "endMinute": 0},
        {"startHour": 6, "startMinute": "0", "endHour": 8, "endMinute": 0},
    ],
)
def test_broken_phase_is_skipped_and_logged(broken: Any, caplog: pytest.LogCaptureFixture) -> None:
    raw = {
        "componentId": "300_3100",
        "componentNumber": 1,
        "type": "CIRCUIT",
        "timeWindowsView": [
            {"id": 1, "weekDay": "MONDAY", "phases": [_phase(6, 0, 8, 0), broken]},
            {"id": 2, "weekDay": "TUESDAY", "phases": [_phase(6, 0, 8, 0)]},
        ],
    }
    with caplog.at_level(logging.WARNING, logger="froeling_lambdatronic.cloud"):
        detail = parse_component_detail(raw)
    assert detail.schedule == {Weekday.MONDAY: (Phase(360, 480),), Weekday.TUESDAY: (Phase(360, 480),)}
    assert detail.schedule_ids == {Weekday.MONDAY: 1, Weekday.TUESDAY: 2}
    assert detail.skipped_phase_days == frozenset({Weekday.MONDAY})
    (record,) = [r for r in caplog.records if r.name == "froeling_lambdatronic.cloud"]
    assert record.levelno == logging.WARNING
    assert "300_3100" in record.getMessage()
    assert "MONDAY" in record.getMessage()
    # Only the component id and the weekday are logged, never the phase values.
    assert "22" not in record.getMessage()
    assert "25" not in record.getMessage()


def test_complete_schedule_has_no_skipped_days() -> None:
    assert parse_component_detail(load("component_300_3100.json")).skipped_phase_days == frozenset()


def test_broken_overview_phase_is_skipped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    data = load("overview.json")
    data["components"][1]["heatingPhase"][0]["phases"].append({"startHour": "x"})
    with caplog.at_level(logging.WARNING, logger="froeling_lambdatronic.cloud"):
        overview = parse_overview(data)
    assert overview.components[1].schedule[Weekday.MONDAY] == (Phase(270, 1320),)
    assert len([r for r in caplog.records if r.name == "froeling_lambdatronic.cloud"]) == 1


@pytest.mark.parametrize("limit", [True, "nan", "inf", "-inf", float("nan")])
def test_odd_parameter_limits_are_ignored(limit: Any) -> None:
    raw = {
        "componentId": "1_100",
        "componentNumber": 1,
        "type": "BOILER",
        "stateView": [{"id": "3_0", "name": "boilerTemp", "minVal": limit, "maxVal": limit}],
    }
    param = parse_component_detail(raw).parameters["boilerTemp"]
    assert (param.minimum, param.maximum) == (None, None)


def test_parse_overview() -> None:
    overview = parse_overview(load("overview.json"))
    assert overview.outside_temperature == "11"
    assert [c.component.key for c in overview.components] == [
        "boiler",
        "hc1",
        "hc2",
        "dhw1",
        "buffer1",
        "feed",
        "circulation",
    ]
    boiler, hc1 = overview.components[0], overview.components[1]
    assert boiler.values["state"] == "3"
    assert boiler.values["boilerOn"] == "0"
    assert boiler.values["ignitionWhenBufferTempBelow"] == "55.0"
    assert "active" not in boiler.values
    assert hc1.component.standard_name == "Heizkreis 01"
    assert hc1.values["circuitPumpControl"] == "1"
    assert hc1.schedule[Weekday.MONDAY] == (Phase(270, 1320),)
    assert overview.components[5].values["pelletsUsageCounter"] == "1.142"
    assert overview.components[5].schedule == {}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"components": {}},
        {"components": ["x"]},
        {
            "components": [
                {"componentId": "1_100", "componentNumber": 1, "type": "BOILER", "heatingPhase": "x"}
            ]
        },
        {"outTemp": "11", "components": []},
    ],
)
def test_malformed_overview(payload: Any) -> None:
    with pytest.raises(CommunicationError):
        parse_overview(payload)


def test_overview_without_outside_temperature() -> None:
    assert parse_overview({"components": []}).outside_temperature is None


def test_parse_system_info() -> None:
    info = parse_system_info(load("info.json"))
    assert info.product_type == "PE1 Pellet 20"
    assert info.equipment_number == "100000001"
    assert info.core_module_version == "V 50.04 B 05.17 (05.04.2019)"
    assert info.touch_version == "V 60.01 B 01.35.30"
    assert info.xml_version == "3.0"
    assert info.facility_structure_since == datetime(2021, 5, 1, 10, 0, 0, 250000, tzinfo=UTC)
    assert info.facility_registered_since == datetime(2020, 1, 1, 8, 0, 0, 500000, tzinfo=UTC)
    assert info.connected_since == datetime(2025, 1, 1, 6, 0, 0, 125000, tzinfo=UTC)
    assert info.last_update == datetime(2025, 1, 2, 12, 0, 0, 750000, tzinfo=UTC)
    assert (info.operation_hours, info.hours_since_last_maintenance) == (12000, 1000)
    assert (info.boiler_starts, info.partial_load_hours, info.hours_in_pellet_mode) == (8000, 500, 10000)
    text = repr(info)
    for private in ("pairing", "password", "Muster", "address", "contacts"):
        assert private not in text


def test_system_info_tolerates_missing_and_odd_values() -> None:
    info = parse_system_info(
        {
            "systemInfo": {
                "productType": {"value": 5},
                "lastUpdate": {"value": "kaputt"},
                "connectedSince": {"value": "2025-01-01T07:00:00+01:00"},
                "xmlVersion": "3.0",
            },
            "operatingData": {"operationHours": {"value": "viele"}, "boilerStarts": {"value": 12}},
        }
    )
    assert info.product_type is None
    assert info.xml_version is None
    assert info.last_update is None
    assert info.connected_since == datetime(2025, 1, 1, 6, 0, tzinfo=UTC)
    assert info.operation_hours is None
    assert info.boiler_starts == 12
    assert parse_system_info({}).equipment_number is None


@pytest.mark.parametrize("payload", [[], {"systemInfo": []}, {"operatingData": "x"}])
def test_malformed_system_info(payload: Any) -> None:
    with pytest.raises(CommunicationError):
        parse_system_info(payload)


def test_parse_header() -> None:
    header = parse_header(load("header.json"))
    assert header == CloudHeader("ONLINE", datetime(2025, 1, 2, 12, 0, 0, 750000, tzinfo=UTC))
    assert header.online
    offline = parse_header({"connectState": "OFFLINE"})
    assert offline == CloudHeader("OFFLINE", None)
    assert not offline.online
    assert parse_header({"connectState": 1, "lastSignal": 5}) == CloudHeader(None, None)
    with pytest.raises(CommunicationError):
        parse_header([])


def test_parse_errors() -> None:
    assert parse_errors(load("errors.json")) == [
        CloudError(
            error_id=1,
            timestamp=datetime(2026, 10, 20, 8, 15, tzinfo=UTC),
            severity="alarm",
            message="Kessel Übertemperatur",
            resolutions="Kesselfühler und Pumpen prüfen",
        ),
        CloudError(
            error_id=2,
            timestamp=datetime(2026, 10, 21, 17, 40, 12, 345000, tzinfo=UTC),
            severity="warning",
            message="Pelletlager Mindestbestand erreicht",
            resolutions="Pellets bestellen",
        ),
    ]
    assert parse_errors([]) == []


def test_parse_errors_skips_broken_entries(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="froeling_lambdatronic.cloud"):
        errors = parse_errors(load("errors_broken.json"))
    assert errors == [
        CloudError(3, datetime(2026, 10, 22, 6, 0, tzinfo=UTC), "warning", "Asche entleeren", None),
        CloudError(None, None, "alarm", "Fühler defekt", None),
        CloudError(None, None, "warning", "Nur Text", None),
    ]
    skipped = [r for r in caplog.records if r.name == "froeling_lambdatronic.cloud"]
    assert len(skipped) == 3
    assert all(r.levelno == logging.WARNING for r in skipped)


def test_parse_errors_requires_a_list() -> None:
    with pytest.raises(CommunicationError):
        parse_errors({"errors": []})


def test_phase_validation() -> None:
    assert Phase(0, 1440).end == 1440
    for start, end in ((-1, 10), (10, 10), (20, 10), (0, 1441)):
        with pytest.raises(ValueError, match="phase"):
            Phase(start, end)


def test_component_keys() -> None:
    def component(kind: ComponentType, number: int) -> CloudComponent:
        return CloudComponent("x", kind, str(kind), number, "", "")

    assert component(ComponentType.BOILER, 1).key == "boiler"
    assert component(ComponentType.CIRCUIT, 3).key == "hc3"
    assert component(ComponentType.DHW, 2).key == "dhw2"
    assert component(ComponentType.BUFFER_TANK, 4).key == "buffer4"
    assert component(ComponentType.FEED_SYSTEM, 1).key == "feed"
    assert component(ComponentType.CIRCULATION_PUMP, 1).key == "circulation"


def test_validate_day() -> None:
    validate_day([])
    validate_day([Phase(0, 60), Phase(60, 120), Phase(180, 240), Phase(1380, 1440)])
    with pytest.raises(ValueError, match="at most 4"):
        validate_day([Phase(i * 60, i * 60 + 30) for i in range(5)])
    with pytest.raises(ValueError, match="overlap"):
        validate_day([Phase(0, 120), Phase(60, 180)])
    with pytest.raises(ValueError, match="sorted"):
        validate_day([Phase(600, 700), Phase(0, 60)])
