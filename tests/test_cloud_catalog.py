"""Tests for the cloud value catalogue and the cloud snapshot."""

import dataclasses
import json
from datetime import time
from pathlib import Path
from typing import Any

import pytest

from froeling_lambdatronic.cloud_catalog import (
    CLOUD_REFS,
    CloudRef,
    build_snapshot,
    cloud_ref,
    equipment_from_components,
    merge_equipment,
)
from froeling_lambdatronic.cloud_models import (
    CloudComponent,
    ComponentType,
    Phase,
    Weekday,
    parse_component_detail,
    parse_component_list,
    parse_overview,
)
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
)

CLOUD = Path(__file__).parent / "fixtures" / "cloud"

# Values Fröling Connect has but Modbus does not (abgleich.md 8.2/8.5, categories C2 and D).
CLOUD_ONLY = {
    "boiler_on",
    "remote_control_allowed",
    "ignition_configured",
    "induced_draft_control",
    "reset_ash_counter",
    "reset_pellet_counter",
    "hc{n}_pump",
    "dhw{n}_mode",
    "buffer{n}_charge",
}


def load(name: str) -> Any:
    return json.loads((CLOUD / name).read_text())


def _empty(cls: type[Any], **values: Any) -> Any:
    return cls(**({f.name: None for f in dataclasses.fields(cls)} | values))


def _modbus_keys() -> set[str]:
    values = _empty(
        LambdatronicValues,
        errors=(),
        heating_circuits={1: _empty(HeatingCircuitValues)},
        hot_water={1: _empty(HotWaterValues)},
        buffers={1: _empty(BufferValues)},
        circulation=_empty(CirculationValues),
        raw={},
    )
    parameters = _empty(
        LambdatronicParameters,
        heating_circuits={1: _empty(HeatingCircuitParameters)},
        hot_water={1: _empty(HotWaterParameters)},
        buffers={1: _empty(BufferParameters)},
        raw={},
    )
    return set(values.flat()) | set(parameters.flat())


def test_templates_match_modbus_keys() -> None:
    modbus = _modbus_keys()
    for template in CLOUD_REFS:
        key = template.format(n=1)
        if template in CLOUD_ONLY:
            assert key not in modbus, key
        else:
            assert key in modbus, key


def test_catalogue_entries() -> None:
    assert CLOUD_REFS["pellet_counter"] == CloudRef(
        ComponentType.FEED_SYSTEM, "pelletsUsageCounter", "number", 1000
    )
    assert CLOUD_REFS["boiler_on"] == CloudRef(ComponentType.BOILER, "boilerOn", "bool", invert=True)
    assert CLOUD_REFS["hc{n}_operating_mode"] == CloudRef(ComponentType.CIRCUIT, "mode", "choice")
    assert CLOUD_REFS["pellet_filling_start_2"].kind == "time"
    names = {ref.name for ref in CLOUD_REFS.values()}
    assert not names & {"desiredRoomTemp", "bufferTankChargeDiskret", "editable"}
    # One cloud parameter belongs to exactly one key.
    pairs = [(ref.component, ref.name) for ref in CLOUD_REFS.values()]
    assert len(pairs) == len(set(pairs))


@pytest.mark.parametrize(
    ("key", "template", "number"),
    [
        ("boiler_temperature", "boiler_temperature", 1),
        ("hc2_flow_temperature", "hc{n}_flow_temperature", 2),
        ("hc18_curve_flow_plus10", "hc{n}_curve_flow_plus10", 18),
        ("dhw1_mode", "dhw{n}_mode", 1),
        ("buffer3_top", "buffer{n}_top", 3),
        ("circulation_pump", "circulation_pump", 1),
        ("hc1_pump", "hc{n}_pump", 1),
        ("pellet_counter", "pellet_counter", 1),
        (
            "buffer1_ignition_below_buffer_temperature",
            "buffer{n}_ignition_below_buffer_temperature",
            1,
        ),
    ],
)
def test_cloud_ref(key: str, template: str, number: int) -> None:
    assert cloud_ref(key) == (CLOUD_REFS[template], number)


@pytest.mark.parametrize(
    "key",
    [
        "hc0_flow_temperature",
        "hc_flow_temperature",
        "hcx_flow_temperature",
        "hc1_desired",
        "x",
        "",
        # The boiler reports only the start difference of buffer 1; buffer 2 must not map onto it.
        "buffer2_ignition_below_buffer_temperature",
    ],
)
def test_cloud_ref_unknown(key: str) -> None:
    assert cloud_ref(key) is None


@pytest.mark.parametrize(
    ("ref", "raw", "expected"),
    [
        (CloudRef(ComponentType.BOILER, "boilerTemp", "number"), "61", 61.0),
        (CloudRef(ComponentType.FEED_SYSTEM, "pelletsUsageCounter", "number", 1000), "2.6", 2600.0),
        (CloudRef(ComponentType.BOILER, "state", "choice"), "19", 19),
        (CloudRef(ComponentType.BOILER, "state", "choice"), "3.0", 3),
        (CloudRef(ComponentType.BOILER, "boilerOn", "bool", invert=True), "0", True),
        (CloudRef(ComponentType.BOILER, "boilerOn", "bool", invert=True), "1", False),
        (CloudRef(ComponentType.CIRCUIT, "circuitPumpControl", "bool"), "1", True),
        (CloudRef(ComponentType.CIRCUIT, "circuitPumpControl", "bool"), "0", False),
        (CloudRef(ComponentType.FEED_SYSTEM, "startFirstFilling", "time"), "10:00", time(10, 0)),
        (CloudRef(ComponentType.FEED_SYSTEM, "startFirstFilling", "time"), "07:05", time(7, 5)),
        (CloudRef(ComponentType.FEED_SYSTEM, "startFirstFilling", "time"), "24:00", None),
        (CloudRef(ComponentType.FEED_SYSTEM, "startFirstFilling", "time"), "", None),
        (CloudRef(ComponentType.BOILER, "boilerTemp", "number"), "-", None),
        (CloudRef(ComponentType.BOILER, "boilerTemp", "number"), "nan", None),
        (CloudRef(ComponentType.BOILER, "state", "choice"), "Heizen", None),
        (CloudRef(ComponentType.BOILER, "boilerOn", "bool"), "JA", None),
    ],
)
def test_decode(ref: CloudRef, raw: str, expected: object) -> None:
    value = ref.decode(raw)
    assert value == expected
    assert type(value) is type(expected)


def test_snapshot_from_overview() -> None:
    snapshot = build_snapshot(parse_overview(load("overview.json")))
    assert not snapshot.details_loaded
    assert [c.key for c in snapshot.components] == [
        "boiler",
        "hc1",
        "hc2",
        "dhw1",
        "buffer1",
        "feed",
        "circulation",
    ]
    expected = {
        "outside_temperature": 11.0,
        "boiler_state": 3,
        "system_state": 2,
        "boiler_temperature": 68.0,
        "boiler_on": True,
        "buffer1_ignition_below_buffer_temperature": 55.0,
        "ignition_configured": False,
        "hc1_operating_mode": 1,
        "hc1_flow_temperature": 44.0,
        "hc1_pump": True,
        "hc2_flow_temperature": 28.0,
        "dhw1_mode": 1,
        "dhw1_setpoint": 55.0,
        "dhw1_pump": 100.0,
        "dhw1_temperature": 55.0,
        "buffer1_top": 63.0,
        "buffer1_bottom": 46.0,
        "buffer1_charge": 66.0,
        "buffer1_pump": 59.0,
        "pellet_stock": 4.5,
        "pellet_consumption_total": 60.5,
        "pellet_counter": 2600.0,
        "circulation_return_temperature": 0.0,
        "circulation_pump": 100.0,
    }
    for key, value in expected.items():
        assert snapshot.value(key) == value, key
    assert snapshot.value("hc1_curve_flow_plus10") is None
    assert snapshot.value("unknown") is None
    assert snapshot.room_sensors == {"hc1": False, "hc2": False}
    assert set(snapshot.values) == set(expected) | {"hc2_operating_mode", "hc2_pump"}
    assert set(snapshot.schedules) == {"hc1", "hc2", "dhw1", "buffer1", "circulation"}
    assert snapshot.schedules["circulation"][Weekday.MONDAY] == (
        Phase(270, 540),
        Phase(660, 780),
        Phase(960, 1320),
    )


def _details() -> list[Any]:
    return [
        parse_component_detail(load(f"component_{c.component_id}.json"))
        for c in parse_component_list(load("component_list.json"))
    ]


def test_snapshot_with_details() -> None:
    snapshot = build_snapshot(parse_overview(load("overview.json")), _details())
    assert snapshot.details_loaded
    # The details (older probe) win over the overview.
    assert snapshot.value("boiler_temperature") == 61.0
    assert snapshot.value("pellet_counter") == 2500.0
    assert snapshot.value("outside_temperature") == 9.0
    assert snapshot.value("pellet_filling_start_1") == time(10, 0)
    assert snapshot.value("pellet_filling_start_2") == time(22, 0)
    assert snapshot.value("auto_feeder_off") is False
    assert snapshot.value("reset_pellet_counter") is False
    assert snapshot.value("reset_ash_counter") is False
    assert snapshot.value("remote_control_allowed") is True
    assert snapshot.value("hc2_curve_flow_plus10") == 29.0
    assert snapshot.value("hc1_heating_limit_setback") == 7.0
    assert snapshot.value("hc1_frost_protection") == 10.0
    assert snapshot.value("hc2_buffer_overheat_protection") == 120.0
    assert snapshot.value("buffer1_circuit_release") == 30.0
    assert snapshot.value("hours_since_maintenance") == 1000.0
    assert snapshot.value("pellet_hopper_level") == 100.0
    assert snapshot.value("circulation_pump_off_above") == 35.0
    assert snapshot.value("circulation_flow_switch") is False
    assert snapshot.value("induced_draft_control") == 0.0
    assert snapshot.schedules["hc1"][Weekday.SATURDAY] == (Phase(360, 1320),)
    assert "boiler" not in snapshot.schedules
    assert len(snapshot.components) == 7


def test_snapshot_keeps_components_only_in_details_and_skips_unknown_types() -> None:
    overview = parse_overview({"components": []})
    solar = parse_component_detail(
        {"componentId": "9_9", "componentNumber": 1, "type": "SOLAR", "stateView": [{"id": "1", "name": "x"}]}
    )
    feed = parse_component_detail(load("component_1_906.json"))
    snapshot = build_snapshot(overview, [solar, feed])
    assert [c.raw_type for c in snapshot.components] == ["SOLAR", "FEED_SYSTEM"]
    assert snapshot.value("pellet_stock") == 4.5
    assert snapshot.value("outside_temperature") is None


def test_snapshot_room_sensors_prefer_known_values_of_later_sources() -> None:
    def circuit(number: int, sub_type: str | None, url_flag: str | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {
            "componentId": f"300_31{number}0",
            "componentNumber": number,
            "type": "CIRCUIT",
        }
        if sub_type is not None:
            item["subType"] = sub_type
        if url_flag is not None:
            item["topView"] = {"pictureUrl": f"https://example.invalid/p?isRoomSensorAvailable={url_flag}"}
        return item

    overview = parse_overview(
        {
            "components": [
                circuit(1, "OUT_TEMP_CRTL"),
                circuit(2, "ROOM_TEMP_CRTL"),
                circuit(3, "OUT_TEMP_CRTL"),
                {"componentId": "1_100", "componentNumber": 1, "type": "BOILER", "subType": "PELLET"},
            ]
        }
    )
    details = [
        parse_component_detail(circuit(1, "OUT_TEMP_CRTL", "true")),
        parse_component_detail(circuit(2, "ROOM_TEMP_CRTL", "false")),
        parse_component_detail(circuit(3, "ROOM_TEMP_CRTL")),  # unknown does not hide a known value
        parse_component_detail(circuit(4, None, "true")),  # only in the details
    ]
    assert build_snapshot(overview).room_sensors == {"hc1": False, "hc2": None, "hc3": False}
    assert build_snapshot(overview, details).room_sensors == {
        "hc1": True,
        "hc2": False,
        "hc3": False,
        "hc4": True,
    }


def test_snapshot_room_sensors_with_fixture_details() -> None:
    snapshot = build_snapshot(parse_overview(load("overview.json")), _details())
    assert snapshot.room_sensors == {"hc1": False, "hc2": False}


def _component(kind: ComponentType | None, number: int, raw: str = "") -> CloudComponent:
    return CloudComponent(f"{raw}{number}", kind, raw or str(kind), number, "", "")


def test_equipment_from_components() -> None:
    components = parse_component_list(load("component_list.json"))
    assert equipment_from_components(components) == Equipment((1, 2), (1,), (1,), circulation_pump=True)
    unordered = [
        _component(ComponentType.CIRCUIT, 3),
        _component(ComponentType.CIRCUIT, 1),
        _component(None, 2, "SOLAR"),
    ]
    assert equipment_from_components(unordered) == Equipment((1, 3), (), ())


def test_merge_equipment() -> None:
    modbus = Equipment((1, 2), (1,), (1,), circulation_pump=True)
    assert merge_equipment(modbus, modbus) == (modbus, ())
    cloud = Equipment((1, 3), (1, 2), (1,), circulation_pump=False)
    merged, deviations = merge_equipment(modbus, cloud)
    assert merged == Equipment((1, 2, 3), (1, 2), (1,), circulation_pump=True)
    assert deviations == ("hc2", "hc3", "dhw2", "circulation")
    assert merge_equipment(Equipment((), (), (2,)), Equipment((), (), ())) == (
        Equipment((), (), (2,)),
        ("buffer2",),
    )
