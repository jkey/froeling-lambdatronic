"""Fröling Connect client (unofficial API of connect-api.froeling.com)."""

import base64
import binascii
import json
import logging
import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import time
from enum import IntEnum
from http import HTTPStatus
from typing import Any

import aiohttp

from .cloud_catalog import CloudRef, CloudSnapshot, build_snapshot, cloud_ref
from .cloud_models import (
    CloudComponent,
    CloudComponentDetail,
    CloudError,
    CloudHeader,
    CloudOverview,
    CloudParameter,
    CloudSystemInfo,
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
from .cloud_models import component_key as _key_of
from .exceptions import (
    AuthenticationError,
    CloudParameterNotFoundError,
    CommunicationError,
    ConnectionFailedError,
    RateLimitedError,
    RemoteControlDisabledError,
    ValueOutOfRangeError,
    WriteRejectedError,
)

DEFAULT_BASE_URL = "https://connect-api.froeling.com"
API_BASE = DEFAULT_BASE_URL
_LOGIN = "/connect/v1.0/resources/login"
_FACILITIES = "/connect/v1.0/resources/service/user/{user_id}/facility"
_FACILITY = "/connect/v1.0/resources/service/user/{user_id}/facility/{facility_id}"
_FACILITY_V2 = "/connect/v2.0/resources/service/user/{user_id}/facility/{facility_id}"
_FCS = "/fcs/v1.0/resources/user/{user_id}/facility/{facility_id}"
# Marker in the per-call set of fresh data: the component list was fetched during this call.
_COMPONENT_LIST = "componentList"
_TIMEOUT = aiohttp.ClientTimeout(total=20)
_LOGGER = logging.getLogger("froeling_lambdatronic.cloud")


class OperatingMode(IntEnum):
    """System operating mode ('Kessel Zustand'), identical to Modbus register 34001 values 0..2."""

    CONTINUOUS_LOAD = 0
    DOMESTIC_HOT_WATER = 1
    AUTOMATIC = 2


@dataclass(frozen=True, slots=True)
class Facility:
    """A facility (Anlage) of the Fröling Connect account."""

    facility_id: int
    name: str
    product_type: str | None
    equipment_number: str | None = None


@dataclass(frozen=True, slots=True)
class CloudBoilerState:
    """Operating mode and remote control permission as reported by Fröling Connect."""

    operating_mode: OperatingMode | None
    remote_control_allowed: bool


class FroelingConnectClient:
    """Talks to connect-api.froeling.com with an injected aiohttp session."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        username: str,
        password: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None
        self._user_id: int | None = None
        # Write caches: parameter ids and limits rarely change, reads refresh them.
        self._components: dict[int, list[CloudComponent]] = {}
        self._details: dict[tuple[int, str], CloudComponentDetail] = {}

    @property
    def user_id(self) -> int | None:
        """User id taken from the token after login."""
        return self._user_id

    async def login(self) -> None:
        """Log in and store the token."""
        body = {"osType": "web", "username": self._username, "password": self._password}
        try:
            async with self._session.post(self._base_url + _LOGIN, json=body, timeout=_TIMEOUT) as response:
                if response.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    raise AuthenticationError("Fröling Connect rejected the credentials")
                if response.status == HTTPStatus.TOO_MANY_REQUESTS:
                    raise _rate_limited(response)
                if response.status != HTTPStatus.OK:
                    raise ConnectionFailedError(f"login failed with HTTP {response.status}")
                token = response.headers.get("Authorization", "")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ConnectionFailedError(f"Fröling Connect not reachable: {err}") from err
        if not token:
            raise _LoginError("login response carries no token")
        self._user_id = _user_id_from_token(token)
        self._token = token

    async def get_facilities(self) -> list[Facility]:
        """List all facilities of the account."""
        data = await self._request("GET", _FACILITIES)
        if not isinstance(data, list):
            raise CommunicationError("facility list is not a list")
        return [_parse_facility(item) for item in data]

    async def get_components(self, facility_id: int) -> list[CloudComponent]:
        """List the components (boiler, heating circuits, hot water tanks, ...) of a facility."""
        components = parse_component_list(
            await self._request("GET", _FCS + "/componentList", facility_id=facility_id)
        )
        self._components[facility_id] = components
        return components

    async def get_component_detail(self, facility_id: int, component_id: str) -> CloudComponentDetail:
        """Read all parameters and the schedule of one component."""
        try:
            data = await self._request("GET", _FCS + "/component/" + component_id, facility_id=facility_id)
        except _NotFoundError:
            # The component id is stale: the next call reloads the component list.
            self._forget(facility_id, component_id)
            raise
        detail = parse_component_detail(data)
        self._details[(facility_id, component_id)] = detail
        return detail

    def _forget(self, facility_id: int, component_id: str) -> None:
        """Drop the cached component list of the facility and the cached detail of the component."""
        self._components.pop(facility_id, None)
        self._details.pop((facility_id, component_id), None)

    async def get_overview(self, facility_id: int) -> CloudOverview:
        """Read the overview: all components with their main values in one request."""
        return parse_overview(await self._request("GET", _FCS + "/overview", facility_id=facility_id))

    async def get_system_info(self, facility_id: int) -> CloudSystemInfo:
        """Read system and operating information (versions, equipment number, operating hours)."""
        return parse_system_info(await self._request("GET", _FACILITY_V2 + "/info", facility_id=facility_id))

    async def get_header(self, facility_id: int) -> CloudHeader:
        """Read the connection state and the time of the last signal."""
        return parse_header(await self._request("GET", _FACILITY + "/header", facility_id=facility_id))

    async def get_errors(self, facility_id: int) -> list[CloudError]:
        """Read the error history including current faults."""
        return parse_errors(await self._request("GET", _FCS + "/error", facility_id=facility_id))

    async def read_snapshot(
        self, facility_id: int, *, details: bool, skip: Collection[str] = ()
    ) -> CloudSnapshot:
        """Read the overview and, with `details`, every component detail except the keys in `skip`.

        Components of an unknown type are not read in detail. A component whose detail cannot be parsed
        or is not found (HTTP 404) is skipped and logged; the snapshot then lacks its detail values.
        Connection, timeout, rate limiting, authentication and login errors abort the snapshot.
        """
        overview = await self.get_overview(facility_id)
        if not details:
            return build_snapshot(overview)
        loaded: list[CloudComponentDetail] = []
        for component in await self.get_components(facility_id):
            if component.key is None or component.key in skip:
                continue
            try:
                loaded.append(await self.get_component_detail(facility_id, component.component_id))
            except _LoginError:
                raise  # a failed login or token renewal is no unreadable detail
            except (CommunicationError, _NotFoundError) as err:
                # Only the component id is logged: the error text carries the URL with user and facility id.
                _LOGGER.warning(
                    "Skipping the detail of component %s: %s", component.component_id, type(err).__name__
                )
        return build_snapshot(overview, loaded)

    async def set_value(self, facility_id: int, key: str, value: float | time) -> None:
        """Write one value by its key (e.g. 'hc1_curve_flow_plus10'), in library units.

        Numbers are checked against the cloud limits, choices against the cloud options; bool and time
        values must be given as `bool` and `datetime.time`.
        """
        resolved = cloud_ref(key)
        if resolved is None:
            raise ValueError(f"{key} has no Fröling Connect parameter")
        ref, number = resolved
        fetched: set[str] = set()
        component = await self._component(facility_id, _key_of(ref.component, number), fetched)
        parameter = _parameter(await self._detail(facility_id, component, fetched), ref.name, key)
        raw = _encode(key, ref, parameter, value)
        await self._ensure_remote_control(facility_id, fetched)
        try:
            await self._put_parameter(facility_id, parameter.id, raw)
        except _NotFoundError:
            # The parameter id changed (e.g. after a facility structure update): reload once. A 404 is
            # never applied by the cloud, so the second PUT does not write the value twice.
            detail = await self.get_component_detail(facility_id, component.component_id)
            parameter = _parameter(detail, ref.name, key)
            try:
                await self._put_parameter(facility_id, parameter.id, _encode(key, ref, parameter, value))
            except _NotFoundError:
                self._forget(facility_id, component.component_id)
                raise

    async def get_schedule(self, facility_id: int, component_key: str) -> Mapping[Weekday, tuple[Phase, ...]]:
        """Read the weekly schedule of a component ('hc1', 'dhw1', 'buffer1', 'circulation')."""
        component = await self._component(facility_id, component_key, set())
        return (await self.get_component_detail(facility_id, component.component_id)).schedule

    async def set_schedule(
        self, facility_id: int, component_key: str, changes: Mapping[Weekday, Sequence[Phase]]
    ) -> None:
        """Replace the phases of the given days; the other days keep their current phases.

        Each changed day may have at most four sorted, non-overlapping phases. If a day that is not
        replaced has a phase that could not be read, nothing is written (CommunicationError): sending
        the week would erase that phase.
        """
        for phases in changes.values():
            validate_day(phases)
        fetched: set[str] = set()
        component = await self._component(facility_id, component_key, fetched)
        detail = await self.get_component_detail(facility_id, component.component_id)
        fetched.add(component.component_id)
        if set(detail.schedule_ids) != set(Weekday):
            raise CloudParameterNotFoundError(f"{component_key} has no weekly schedule")
        kept_unreadable = [
            day.value for day in Weekday if day in detail.skipped_phase_days and day not in changes
        ]
        if kept_unreadable:
            raise CommunicationError(
                f"{component_key} has unreadable phases on {', '.join(kept_unreadable)}; "
                "writing the week would erase them"
            )
        week = {day: tuple(detail.schedule.get(day, ())) for day in Weekday}
        week.update({day: tuple(phases) for day, phases in changes.items()})
        await self._ensure_remote_control(facility_id, fetched)
        body = [
            {
                "id": detail.schedule_ids[day],
                "weekDay": day.value,
                "phases": [_phase_json(p) for p in week[day]],
            }
            for day in Weekday
        ]
        await self._request("POST", _FCS + "/timeWindows", facility_id=facility_id, json_body=body)

    async def _component(self, facility_id: int, key: str, fetched: set[str]) -> CloudComponent:
        components = self._components.get(facility_id)
        found = next((c for c in components or () if c.key == key), None)
        if found is None and _COMPONENT_LIST not in fetched:
            components = await self.get_components(facility_id)
            fetched.add(_COMPONENT_LIST)
            found = next((c for c in components if c.key == key), None)
        if found is None:
            raise CloudParameterNotFoundError(f"component {key} not found in the facility")
        return found

    async def _detail(
        self, facility_id: int, component: CloudComponent, fetched: set[str]
    ) -> CloudComponentDetail:
        detail = self._details.get((facility_id, component.component_id))
        if detail is None:
            detail = await self.get_component_detail(facility_id, component.component_id)
            fetched.add(component.component_id)
        return detail

    async def _ensure_remote_control(self, facility_id: int, fetched: set[str]) -> None:
        boiler = await self._component(facility_id, _key_of(ComponentType.BOILER, 1), fetched)
        detail = await self._detail(facility_id, boiler, fetched)
        if not _remote_on(detail) and boiler.component_id not in fetched:
            # The cached permission may be outdated: check once more before refusing.
            detail = await self.get_component_detail(facility_id, boiler.component_id)
        if not _remote_on(detail):
            raise RemoteControlDisabledError("'Fernschalten über connect möglich' is disabled")

    async def _put_parameter(self, facility_id: int, parameter_id: str, value: str) -> None:
        url = _FCS + "/parameter/" + parameter_id
        await self._request("PUT", url, facility_id=facility_id, json_body={"value": value})

    async def get_boiler_state(self, facility_id: int) -> CloudBoilerState:
        """Read operating mode and remote control permission."""
        params = await self._boiler_config_params(facility_id)
        mode_value = params["mode2"].get("value")
        try:
            mode: OperatingMode | None = OperatingMode(int(str(mode_value)))
        except (TypeError, ValueError):
            mode = None
        return CloudBoilerState(mode, str(params["remoteOn"].get("value")) == "1")

    async def set_operating_mode(self, facility_id: int, mode: OperatingMode) -> None:
        """Switch the system operating mode."""
        params = await self._boiler_config_params(facility_id)
        if str(params["remoteOn"].get("value")) != "1":
            raise RemoteControlDisabledError("'Fernschalten über connect möglich' is disabled")
        parameter_id = params["mode2"].get("id")
        if parameter_id is None:
            raise CommunicationError("parameter 'mode2' has no id")
        url = _FCS + "/parameter/" + str(parameter_id)
        await self._request("PUT", url, facility_id=facility_id, json_body={"value": str(int(mode))})

    async def _boiler_config_params(self, facility_id: int) -> dict[str, dict[str, Any]]:
        components = await self._request("GET", _FCS + "/componentList", facility_id=facility_id)
        if not isinstance(components, list) or not all(isinstance(c, dict) for c in components):
            raise CommunicationError("component list is not a list of objects")
        boiler = next((c for c in components if c.get("type") == "BOILER"), None)
        if boiler is None:
            raise CloudParameterNotFoundError("no boiler component in the facility")
        component_id = boiler.get("componentId")
        if component_id is None:
            raise CommunicationError("boiler component has no componentId")
        detail = await self._request("GET", _FCS + "/component/" + str(component_id), facility_id=facility_id)
        if not isinstance(detail, dict):
            raise CommunicationError("component detail is not an object")
        top_view = detail.get("topView") or {}
        if not isinstance(top_view, dict):
            raise CommunicationError("component topView is not an object")
        config = top_view.get("configParams") or {}
        if not isinstance(config, dict):
            raise CommunicationError("component configParams is not an object")
        params: dict[str, dict[str, Any]] = {}
        for name in ("mode2", "remoteOn"):
            if name not in config:
                raise CloudParameterNotFoundError(f"parameter '{name}' not found")
            param = config[name]
            if not isinstance(param, dict):
                raise CommunicationError(f"parameter '{name}' is not an object")
            params[name] = param
        return params

    async def _request(
        self,
        method: str,
        url_template: str,
        *,
        facility_id: int | None = None,
        json_body: Any = None,  # noqa: ANN401 - JSON payloads are untyped
    ) -> Any:  # noqa: ANN401 - JSON payloads are untyped
        if self._token is None:
            await self.login()
        for attempt in range(2):
            url = self._base_url + url_template.format(user_id=self._user_id, facility_id=facility_id)
            try:
                async with self._session.request(
                    method,
                    url,
                    headers={"Authorization": self._token or ""},
                    timeout=_TIMEOUT,
                    json=json_body,
                ) as response:
                    if response.status != HTTPStatus.UNAUTHORIZED:
                        return await self._handle_response(method, url, response)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:  # JSON and text decoding errors
                raise ConnectionFailedError(f"Fröling Connect request failed: {err}") from err
            # 401: renew the token once, outside the closed response context.
            if attempt == 0:
                await self.login()
        raise AuthenticationError("token renewal did not help")

    @staticmethod
    async def _handle_response(method: str, url: str, response: aiohttp.ClientResponse) -> Any:  # noqa: ANN401 - JSON payloads are untyped
        if response.status == HTTPStatus.NOT_MODIFIED:
            # Fröling Connect answers an unchanged write with 304; a read must carry data.
            if method in ("PUT", "POST"):
                return None
            raise CommunicationError(f"{method} {url} answered 304 without data")
        if response.status == HTTPStatus.TOO_MANY_REQUESTS:
            raise _rate_limited(response)
        if response.status == HTTPStatus.NOT_FOUND:
            raise _NotFoundError(f"{method} {url} failed with HTTP 404")
        if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
            raise ConnectionFailedError(f"{method} {url} failed with HTTP {response.status}")
        return await response.json(content_type=None)


class _LoginError(CommunicationError):
    """The login answer carries no usable token; read_snapshot must not skip it as unreadable detail."""


class _NotFoundError(ConnectionFailedError):
    """HTTP 404; lets set_value reload a stale parameter id."""


def _rate_limited(response: aiohttp.ClientResponse) -> RateLimitedError:
    header = response.headers.get("Retry-After", "").strip()
    # Retry-After may also be an HTTP date; only the seconds form is used.
    return RateLimitedError(float(header) if header.isascii() and header.isdigit() else None)


def _parameter(detail: CloudComponentDetail, name: str, key: str) -> CloudParameter:
    parameter = detail.parameters.get(name)
    if parameter is None:
        raise CloudParameterNotFoundError(f"parameter '{name}' for {key} not found")
    return parameter


def _remote_on(detail: CloudComponentDetail) -> bool:
    return _parameter(detail, "remoteOn", "remote control").value == "1"


def _encode(key: str, ref: CloudRef, parameter: CloudParameter, value: float | time) -> str:
    """Validate a value in library units and convert it to the string Fröling Connect expects."""
    if not parameter.editable:
        raise WriteRejectedError(key, "not editable")
    if ref.kind == "time":
        if not isinstance(value, time):
            raise TypeError(f"{key} expects a datetime.time")
        if value.second or value.microsecond:
            raise ValueError(f"{key} accepts whole minutes only, got {value.isoformat()}")
        return f"{value.hour:02d}:{value.minute:02d}"
    if ref.kind == "bool":
        if not isinstance(value, bool):
            raise TypeError(f"{key} expects a bool")
        return "1" if value != ref.invert else "0"
    if isinstance(value, bool | time) or (ref.kind == "choice" and not isinstance(value, int)):
        raise TypeError(f"{key} expects {'an int' if ref.kind == 'choice' else 'a number'}")
    if ref.kind == "choice":
        allowed = str(value) in parameter.options if parameter.options else _in_range(value, parameter)
        if not allowed:
            raise ValueOutOfRangeError(key, value, parameter.minimum, parameter.maximum)
        return str(value)
    cloud_value = value / ref.factor
    if not math.isfinite(cloud_value) or not _in_range(cloud_value, parameter):
        raise ValueOutOfRangeError(
            key, value, _scaled(parameter.minimum, ref), _scaled(parameter.maximum, ref)
        )
    return _number_text(cloud_value)


def _in_range(value: float, parameter: CloudParameter) -> bool:
    return (parameter.minimum is None or value >= parameter.minimum) and (
        parameter.maximum is None or value <= parameter.maximum
    )


def _scaled(limit: float | None, ref: CloudRef) -> float | None:
    return None if limit is None else limit * ref.factor


def _number_text(value: float) -> str:
    """Shortest decimal text: 47 -> '47', 4.5 -> '4.5', -2.5 -> '-2.5'."""
    text = f"{round(value, 6):f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _phase_json(phase: Phase) -> dict[str, int]:
    # 1440 minutes is sent as 24:00, the end of the day.
    return {
        "startHour": phase.start // 60,
        "startMinute": phase.start % 60,
        "endHour": phase.end // 60,
        "endMinute": phase.end % 60,
    }


def _parse_facility(item: Any) -> Facility:  # noqa: ANN401 - untyped JSON item
    if not isinstance(item, dict):
        raise CommunicationError("facility entry is not an object")
    try:
        facility_id = int(item["facilityId"])
    except (KeyError, TypeError, ValueError) as err:
        raise CommunicationError("facility entry has no valid facilityId") from err
    info = item.get("protocol3200Info") or {}
    if not isinstance(info, dict):
        raise CommunicationError("facility protocol3200Info is not an object")
    product_type = info.get("productType")
    if product_type is not None and not isinstance(product_type, str):
        raise CommunicationError("facility productType is not a string")
    equipment_number = item.get("equipmentNumber")
    if equipment_number is not None and not isinstance(equipment_number, str):
        raise CommunicationError("facility equipmentNumber is not a string")
    return Facility(
        facility_id=facility_id,
        name=str(item.get("name", "")),
        product_type=product_type,
        equipment_number=equipment_number,
    )


def _user_id_from_token(token: str) -> int:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return int(data["userId"])
    except (IndexError, KeyError, ValueError, binascii.Error, TypeError) as err:
        raise _LoginError("unexpected token format") from err
