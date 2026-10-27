"""Data models and tolerant parsers for Fröling Connect responses (unofficial API)."""

import itertools
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from .exceptions import CommunicationError
from .models import MINUTES_PER_DAY

_LOGGER = logging.getLogger("froeling_lambdatronic.cloud")

# Fröling Connect accepts at most four phases per day for Lambdatronic 3200 (GEN_3200).
MAX_PHASES_PER_DAY = 4

# Views of a component detail in ascending priority: a later view wins for the same parameter name.
_VIEW_PRIORITY = ("stateView", "topView", "setupView")
_TOP_VIEW_GROUPS = ("pictureParams", "infoParams", "configParams")


class ComponentType(StrEnum):
    """Component types of a Lambdatronic 3200 facility in Fröling Connect."""

    BOILER = "BOILER"
    CIRCUIT = "CIRCUIT"
    DHW = "DHW"
    BUFFER_TANK = "BUFFER_TANK"
    FEED_SYSTEM = "FEED_SYSTEM"
    CIRCULATION_PUMP = "CIRCULATION_PUMP"


class Weekday(StrEnum):
    """Day of a weekly schedule, spelled as in the API."""

    MONDAY = "MONDAY"
    TUESDAY = "TUESDAY"
    WEDNESDAY = "WEDNESDAY"
    THURSDAY = "THURSDAY"
    FRIDAY = "FRIDAY"
    SATURDAY = "SATURDAY"
    SUNDAY = "SUNDAY"


@dataclass(frozen=True, slots=True, order=True)
class Phase:
    """One switch-on phase of a day in minutes after midnight; end 1440 means 24:00."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not 0 <= self.start < self.end <= MINUTES_PER_DAY:
            raise ValueError(f"phase {self.start}..{self.end} must satisfy 0 <= start < end <= 1440")


def validate_day(phases: Sequence[Phase]) -> None:
    """Check one day of a schedule: at most four phases, sorted and not overlapping."""
    if len(phases) > MAX_PHASES_PER_DAY:
        raise ValueError(f"{len(phases)} phases given, at most {MAX_PHASES_PER_DAY} are allowed per day")
    for previous, current in itertools.pairwise(phases):
        if current.start < previous.end:
            raise ValueError(
                f"phases must be sorted and must not overlap: {previous.start}..{previous.end} "
                f"and {current.start}..{current.end}"
            )


_COMPONENT_KEYS = {
    ComponentType.BOILER: "boiler",
    ComponentType.CIRCUIT: "hc{n}",
    ComponentType.DHW: "dhw{n}",
    ComponentType.BUFFER_TANK: "buffer{n}",
    ComponentType.FEED_SYSTEM: "feed",
    ComponentType.CIRCULATION_PUMP: "circulation",
}


@dataclass(frozen=True, slots=True)
class CloudComponent:
    """A component of the facility; `number` matches the Modbus numbering (heating circuit 2 = 2)."""

    component_id: str
    type: ComponentType | None
    raw_type: str
    number: int
    standard_name: str
    display_name: str

    @property
    def key(self) -> str | None:
        """Key prefix of the value keys: 'boiler', 'hc1', 'dhw1', 'buffer1', 'feed' or 'circulation'."""
        if self.type is None:
            return None
        return component_key(self.type, self.number)


def component_key(component_type: ComponentType, number: int) -> str:
    """Key of a component, e.g. (CIRCUIT, 2) -> 'hc2'; boiler, feed and circulation carry no number."""
    return _COMPONENT_KEYS[component_type].format(n=number)


@dataclass(frozen=True, slots=True)
class CloudParameter:
    """One parameter of a component detail; `value` is the raw string of the API."""

    id: str
    name: str
    display_name: str
    value: str
    unit: str
    minimum: float | None
    maximum: float | None
    editable: bool
    kind: str
    options: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class CloudComponentDetail:
    """All parameters and the weekly schedule of one component.

    `skipped_phase_days` lists the days with a phase that could not be read and is missing in `schedule`.
    """

    component: CloudComponent
    parameters: Mapping[str, CloudParameter]
    schedule: Mapping[Weekday, tuple[Phase, ...]]
    schedule_ids: Mapping[Weekday, int]
    skipped_phase_days: frozenset[Weekday] = frozenset()


@dataclass(frozen=True, slots=True)
class CloudOverviewComponent:
    """A component as listed by the overview: raw string values by parameter name, without ids."""

    component: CloudComponent
    values: Mapping[str, str]
    schedule: Mapping[Weekday, tuple[Phase, ...]]


@dataclass(frozen=True, slots=True)
class CloudOverview:
    """The facility overview: outside temperature and all components with a subset of their values."""

    outside_temperature: str | None
    components: tuple[CloudOverviewComponent, ...]


@dataclass(frozen=True, slots=True)
class CloudSystemInfo:
    """System and operating information (UTC timestamps); address, contacts and pairing code are dropped."""

    product_type: str | None
    equipment_number: str | None
    core_module_version: str | None
    touch_version: str | None
    xml_version: str | None
    facility_structure_since: datetime | None
    facility_registered_since: datetime | None
    connected_since: datetime | None
    last_update: datetime | None
    operation_hours: int | None
    hours_since_last_maintenance: int | None
    boiler_starts: int | None
    partial_load_hours: int | None
    hours_in_pellet_mode: int | None


@dataclass(frozen=True, slots=True)
class CloudHeader:
    """Connection state of the facility as seen by Fröling Connect."""

    connect_state: str | None
    last_signal: datetime | None

    @property
    def online(self) -> bool:
        """True if Fröling Connect reports the facility as online."""
        return self.connect_state == "ONLINE"


@dataclass(frozen=True, slots=True)
class CloudError:
    """One entry of the error history (current faults included)."""

    error_id: int | None
    timestamp: datetime | None
    severity: Literal["warning", "alarm"]
    message: str
    resolutions: str | None


def parse_component_list(data: Any) -> list[CloudComponent]:  # noqa: ANN401 - untyped JSON
    """Parse the response of `componentList`."""
    if not isinstance(data, list):
        raise CommunicationError("component list is not a list")
    return [_component(item) for item in data]


def parse_component_detail(data: Any) -> CloudComponentDetail:  # noqa: ANN401 - untyped JSON
    """Parse the response of `component/{id}`; parameters of all views are merged by name."""
    if not isinstance(data, dict):
        raise CommunicationError("component detail is not an object")
    component = _component(data)
    parameters: dict[str, CloudParameter] = {}
    for view in _VIEW_PRIORITY:
        for raw in _view_parameters(data.get(view), view):
            parameter = _parameter(raw)
            parameters[parameter.name] = parameter
    schedule, schedule_ids, skipped = _schedule(data.get("timeWindowsView"), component, with_ids=True)
    return CloudComponentDetail(component, parameters, schedule, schedule_ids, skipped)


def parse_overview(data: Any) -> CloudOverview:  # noqa: ANN401 - untyped JSON
    """Parse the response of `overview`."""
    if not isinstance(data, dict) or not isinstance(data.get("components"), list):
        raise CommunicationError("overview has no component list")
    outside = data.get("outTemp")
    if outside is not None and not isinstance(outside, dict):
        raise CommunicationError("overview outTemp is not an object")
    components: list[CloudOverviewComponent] = []
    for item in data["components"]:
        component = _component(item)
        values = {
            name: str(entry["value"])
            for name, entry in item.items()
            if isinstance(entry, dict) and entry.get("value") is not None
        }
        schedule, _, _ = _schedule(item.get("heatingPhase"), component, with_ids=False)
        components.append(CloudOverviewComponent(component, values, schedule))
    temperature = None if outside is None or outside.get("value") is None else str(outside["value"])
    return CloudOverview(temperature, tuple(components))


def parse_system_info(data: Any) -> CloudSystemInfo:  # noqa: ANN401 - untyped JSON
    """Parse the response of `info` (v2.0); only technical fields are kept."""
    if not isinstance(data, dict):
        raise CommunicationError("system info is not an object")
    system = _section(data, "systemInfo")
    operating = _section(data, "operatingData")
    return CloudSystemInfo(
        product_type=_text(system, "productType"),
        equipment_number=_text(system, "equipmentNumber"),
        core_module_version=_text(system, "coreModuleVersion"),
        touch_version=_text(system, "touchVersion"),
        xml_version=_text(system, "xmlVersion"),
        facility_structure_since=_timestamp(_text(system, "facilityStructureSince")),
        facility_registered_since=_timestamp(_text(system, "facilityRegisteredSince")),
        connected_since=_timestamp(_text(system, "connectedSince")),
        last_update=_timestamp(_text(system, "lastUpdate")),
        operation_hours=_integer(operating, "operationHours"),
        hours_since_last_maintenance=_integer(operating, "hoursSinceLastMaintenance"),
        boiler_starts=_integer(operating, "boilerStarts"),
        partial_load_hours=_integer(operating, "partialLoadHours"),
        hours_in_pellet_mode=_integer(operating, "hoursInPelletMode"),
    )


def parse_header(data: Any) -> CloudHeader:  # noqa: ANN401 - untyped JSON
    """Parse the response of `header`."""
    if not isinstance(data, dict):
        raise CommunicationError("facility header is not an object")
    state = data.get("connectState")
    signal = data.get("lastSignal")
    return CloudHeader(
        connect_state=state if isinstance(state, str) else None,
        last_signal=_timestamp(signal) if isinstance(signal, str) else None,
    )


def parse_errors(data: Any) -> list[CloudError]:  # noqa: ANN401 - untyped JSON
    """Parse the error history; a broken entry is skipped and logged instead of failing the whole list."""
    if not isinstance(data, list):
        raise CommunicationError("error list is not a list")
    errors: list[CloudError] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("message"), str):
            _LOGGER.warning("Skipping error entry %d: not an object with a message", index)
            continue
        error_id = item.get("errorId")
        date_time = item.get("dateTime")
        resolutions = item.get("resolutions")
        errors.append(
            CloudError(
                error_id=error_id if isinstance(error_id, int) and not isinstance(error_id, bool) else None,
                timestamp=_timestamp(date_time) if isinstance(date_time, str) else None,
                # The portal shows type 2 as warning and everything else as alarm.
                severity="warning" if str(item.get("type")) == "2" else "alarm",
                message=item["message"],
                resolutions=resolutions if isinstance(resolutions, str) else None,
            )
        )
    return errors


def _component(item: Any) -> CloudComponent:  # noqa: ANN401 - untyped JSON
    if not isinstance(item, dict):
        raise CommunicationError("component is not an object")
    component_id = item.get("componentId")
    raw_type = item.get("type")
    number = item.get("componentNumber")
    if not isinstance(component_id, str) or not isinstance(raw_type, str):
        raise CommunicationError("component has no componentId or type")
    if not isinstance(number, int) or isinstance(number, bool):
        raise CommunicationError(f"component {component_id} has no valid componentNumber")
    try:
        component_type: ComponentType | None = ComponentType(raw_type)
    except ValueError:
        component_type = None
    # The overview carries the standard name ("Heizkreis 01") as displayCategory.
    standard_name = item.get("standardName") or item.get("displayCategory") or ""
    return CloudComponent(
        component_id=component_id,
        type=component_type,
        raw_type=raw_type,
        number=number,
        standard_name=str(standard_name),
        display_name=str(item.get("displayName") or ""),
    )


def _view_parameters(view: Any, name: str) -> list[Any]:  # noqa: ANN401 - untyped JSON
    if view is None:
        return []
    if name != "topView":
        if not isinstance(view, list):
            raise CommunicationError(f"{name} is not a list")
        return view
    if not isinstance(view, dict):
        raise CommunicationError("topView is not an object")
    parameters: list[Any] = []
    for group in _TOP_VIEW_GROUPS:
        # Empty groups are sent as [] instead of {}.
        params = view.get(group) or {}
        if not isinstance(params, dict):
            raise CommunicationError(f"topView {group} is not an object")
        parameters += params.values()
    return parameters


def _parameter(raw: Any) -> CloudParameter:  # noqa: ANN401 - untyped JSON
    if not isinstance(raw, dict):
        raise CommunicationError("parameter is not an object")
    parameter_id = raw.get("id")
    name = raw.get("name")
    if not isinstance(parameter_id, str) or not isinstance(name, str):
        raise CommunicationError("parameter has no id or name")
    options = raw.get("stringListKeyValues") or {}
    if not isinstance(options, dict):
        raise CommunicationError(f"parameter {name} has invalid options")
    value = raw.get("value")
    return CloudParameter(
        id=parameter_id,
        name=name,
        display_name=str(raw.get("displayName") or ""),
        value="" if value is None else str(value),
        unit=str(raw.get("unit") or ""),
        minimum=_number(raw.get("minVal")),
        maximum=_number(raw.get("maxVal")),
        editable=raw.get("editable") is True,
        kind=str(raw.get("parameterType") or ""),
        options={str(key): str(text) for key, text in options.items()},
    )


def _schedule(
    data: Any,  # noqa: ANN401 - untyped JSON
    component: CloudComponent,
    *,
    with_ids: bool,
) -> tuple[dict[Weekday, tuple[Phase, ...]], dict[Weekday, int], frozenset[Weekday]]:
    schedule: dict[Weekday, tuple[Phase, ...]] = {}
    ids: dict[Weekday, int] = {}
    skipped: set[Weekday] = set()
    if data is None:
        return schedule, ids, frozenset()
    if not isinstance(data, list):
        raise CommunicationError("schedule is not a list")
    for day in data:
        if not isinstance(day, dict) or not isinstance(day.get("phases"), list):
            raise CommunicationError("schedule day is not an object with phases")
        try:
            weekday = Weekday(str(day.get("weekDay")))
        except ValueError:
            continue
        phases: list[Phase] = []
        for raw in day["phases"]:
            try:
                phase = _phase(raw)
            except ValueError:
                # Only component id and weekday are logged; phase values stay out of the log.
                _LOGGER.warning(
                    "Skipping an unreadable schedule phase of component %s on %s",
                    component.component_id,
                    weekday.value,
                )
                skipped.add(weekday)
                continue
            if phase is not None:
                phases.append(phase)
        schedule[weekday] = tuple(phases)
        if with_ids:
            day_id = day.get("id")
            if not isinstance(day_id, int) or isinstance(day_id, bool):
                raise CommunicationError(f"schedule day {weekday} has no valid id")
            ids[weekday] = day_id
    return schedule, ids, frozenset(skipped)


_PHASE_FIELDS = ("startHour", "startMinute", "endHour", "endMinute")


def _phase(raw: Any) -> Phase | None:  # noqa: ANN401 - untyped JSON
    """One phase; None for an unused slot (start == end), ValueError if it cannot be read."""
    if not isinstance(raw, dict):
        raise ValueError("phase is not an object")
    fields: list[int] = []
    for name in _PHASE_FIELDS:
        field = raw.get(name)
        if not isinstance(field, int) or isinstance(field, bool):
            raise ValueError("phase fields are not integers")
        fields.append(field)
    start_hour, start_minute, end_hour, end_minute = fields
    start = start_hour * 60 + start_minute
    end = end_hour * 60 + end_minute
    if start == end:
        return None  # unused slot
    return Phase(start, end)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    section = data.get(name)
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise CommunicationError(f"system info {name} is not an object")
    return section


def _text(section: dict[str, Any], key: str) -> str | None:
    entry = section.get(key)
    value = entry.get("value") if isinstance(entry, dict) else None
    return value if isinstance(value, str) else None


def _integer(section: dict[str, Any], key: str) -> int | None:
    entry = section.get(key)
    value = entry.get("value") if isinstance(entry, dict) else None
    try:
        return int(value) if isinstance(value, int | str) and not isinstance(value, bool) else None
    except ValueError:
        return None


def _number(value: Any) -> float | None:  # noqa: ANN401 - untyped JSON
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: str | None) -> datetime | None:
    """Parse '2025-01-02T12:00:00.750', '2020-01-01 08:00:00.500' or '…Z'; naive values are UTC."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
