"""Tests for the Fröling Connect client against a real local aiohttp server."""

import asyncio
import base64
import json
import logging
import socket
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from froeling_lambdatronic import cloud
from froeling_lambdatronic.cloud import (
    CloudBoilerState,
    Facility,
    FroelingConnectClient,
    OperatingMode,
)
from froeling_lambdatronic.cloud_models import Phase, Weekday
from froeling_lambdatronic.exceptions import (
    AuthenticationError,
    CloudParameterNotFoundError,
    CommunicationError,
    ConnectionFailedError,
    RateLimitedError,
    RemoteControlDisabledError,
    ValueOutOfRangeError,
    WriteRejectedError,
)

USER = 12345
FACILITY = 777
LOGIN = "/connect/v1.0/resources/login"
FACILITIES = f"/connect/v1.0/resources/service/user/{USER}/facility"
COMPONENTS = f"/fcs/v1.0/resources/user/{USER}/facility/{FACILITY}/componentList"
COMPONENT = f"/fcs/v1.0/resources/user/{USER}/facility/{FACILITY}/component/1_100"
PARAMETER = f"/fcs/v1.0/resources/user/{USER}/facility/{FACILITY}/parameter/100011_9963"
FCS = f"/fcs/v1.0/resources/user/{USER}/facility/{FACILITY}"
OVERVIEW = FCS + "/overview"
TIME_WINDOWS = FCS + "/timeWindows"
INFO = f"/connect/v2.0/resources/service/user/{USER}/facility/{FACILITY}/info"
HEADER = f"/connect/v1.0/resources/service/user/{USER}/facility/{FACILITY}/header"
ERRORS = FCS + "/error"
CLOUD_FIXTURES = Path(__file__).parent / "fixtures" / "cloud"


@dataclass
class Reply:
    status: int = 200
    payload: Any = None
    body: str | bytes | None = None
    content_type: str = "application/json"
    headers: dict[str, str] = field(default_factory=dict)
    delay: float = 0.0


@dataclass
class Received:
    method: str
    path: str
    headers: dict[str, str]
    json: Any


class ServerState:
    """Per-route queued replies (consumed in order) and the requests received."""

    def __init__(self) -> None:
        self.replies: defaultdict[tuple[str, str], deque[Reply]] = defaultdict(deque)
        self.requests: list[Received] = []

    def add(self, method: str, path: str, reply: Reply) -> None:
        self.replies[(method, path)].append(reply)

    def received(self, method: str, path: str) -> list[Received]:
        return [r for r in self.requests if (r.method, r.path) == (method, path)]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.json() if request.can_read_body else None
        self.requests.append(Received(request.method, request.path, dict(request.headers), body))
        queue = self.replies[(request.method, request.path)]
        if not queue:
            return web.Response(status=599, text="no reply queued")
        reply = queue.popleft()
        if reply.delay:
            await asyncio.sleep(reply.delay)
        if reply.body is not None:
            content = reply.body.encode() if isinstance(reply.body, str) else reply.body
            return web.Response(
                status=reply.status, body=content, content_type=reply.content_type, headers=reply.headers
            )
        if reply.payload is not None:
            return web.json_response(reply.payload, status=reply.status, headers=reply.headers)
        return web.Response(status=reply.status, headers=reply.headers)


def token(user_id: int = USER) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"userId": user_id}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.signature"


def component(mode: str = "2", remote: str = "1", *, with_mode: bool = True) -> dict[str, Any]:
    config: dict[str, Any] = {
        "remoteOn": {
            "id": "8_1085",
            "name": "remoteOn",
            "editable": True,
            "value": remote,
            "stringListKeyValues": {"0": "NEIN", "1": "JA"},
        },
    }
    if with_mode:
        config["mode2"] = {
            "id": "100011_9963",
            "name": "mode2",
            "editable": True,
            "value": mode,
            "stringListKeyValues": {"0": "Dauerlast", "1": "Brauchwasser", "2": "Automatik"},
        }
    return {"componentId": "1_100", "type": "BOILER", "topView": {"configParams": config}}


COMPONENT_LIST = [
    {"componentId": "300_3100", "type": "CIRCUIT"},
    {"componentId": "1_100", "type": "BOILER"},
]


@dataclass
class Env:
    session: aiohttp.ClientSession
    base_url: str
    state: ServerState

    def client(self, password: str = "pw") -> FroelingConnectClient:
        return FroelingConnectClient(self.session, "user", password, base_url=self.base_url)


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    state = ServerState()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", state.handle)
    async with TestServer(app) as server, aiohttp.ClientSession() as session:
        yield Env(session, str(server.make_url("")).rstrip("/"), state)


def login_ok(state: ServerState, user_id: int = USER) -> None:
    state.add("POST", LOGIN, Reply(payload={"userData": {}}, headers={"Authorization": token(user_id)}))


def closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


async def test_login_sets_user_id(env: Env) -> None:
    login_ok(env.state)
    client = env.client()
    await client.login()
    assert client.user_id == USER
    assert env.state.received("POST", LOGIN)[0].json == {
        "osType": "web",
        "username": "user",
        "password": "pw",
    }


@pytest.mark.parametrize("status", [401, 403])
async def test_login_rejected(env: Env, status: int) -> None:
    env.state.add("POST", LOGIN, Reply(status=status))
    with pytest.raises(AuthenticationError):
        await env.client("bad").login()


async def test_login_server_error_and_network_error(env: Env) -> None:
    env.state.add("POST", LOGIN, Reply(status=500))
    with pytest.raises(ConnectionFailedError):
        await env.client().login()
    down = FroelingConnectClient(env.session, "user", "pw", base_url=closed_port_url())
    with pytest.raises(ConnectionFailedError):
        await down.login()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "not-a-jwt"},
        {"Authorization": "a.bm90LWpzb24.c"},
        {"Authorization": "a." + base64.urlsafe_b64encode(b"[1]").decode() + ".c"},
    ],
)
async def test_login_without_usable_token(env: Env, headers: dict[str, str]) -> None:
    env.state.add("POST", LOGIN, Reply(headers=headers))
    with pytest.raises(CommunicationError):
        await env.client().login()


async def test_default_base_url() -> None:
    async with aiohttp.ClientSession() as session:
        client = FroelingConnectClient(session, "user", "pw")
        assert client._base_url == cloud.DEFAULT_BASE_URL == "https://connect-api.froeling.com"


async def test_get_facilities(env: Env) -> None:
    login_ok(env.state)
    env.state.add(
        "GET",
        FACILITIES,
        Reply(
            payload=[
                {
                    "facilityId": FACILITY,
                    "name": "Zuhause",
                    "protocol3200Info": {"productType": "PE1 Pellet 20"},
                },
                {"facilityId": 9, "name": "Andere"},
            ]
        ),
    )
    assert await env.client().get_facilities() == [
        Facility(FACILITY, "Zuhause", "PE1 Pellet 20"),
        Facility(9, "Andere", None),
    ]


async def test_get_boiler_state(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload=component("1", "1")))
    assert await env.client().get_boiler_state(FACILITY) == CloudBoilerState(
        OperatingMode.DOMESTIC_HOT_WATER, True
    )


async def test_unknown_mode_value(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload=component("9", "0")))
    state = await env.client().get_boiler_state(FACILITY)
    assert state == CloudBoilerState(None, False)


async def test_missing_parameters(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", COMPONENTS, Reply(payload=[{"componentId": "300_3100", "type": "CIRCUIT"}]))
    client = env.client()
    with pytest.raises(CloudParameterNotFoundError):
        await client.get_boiler_state(FACILITY)
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload=component(with_mode=False)))
    with pytest.raises(CloudParameterNotFoundError):
        await client.get_boiler_state(FACILITY)


async def test_set_operating_mode(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload=component("2", "1")))
    env.state.add("PUT", PARAMETER, Reply(payload={"value": "1"}))
    await env.client().set_operating_mode(FACILITY, OperatingMode.DOMESTIC_HOT_WATER)
    put = env.state.received("PUT", PARAMETER)[0]
    assert put.json == {"value": "1"}
    assert put.headers["Authorization"] == token()


async def test_set_operating_mode_not_modified(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload=component("2", "1")))
    env.state.add("PUT", PARAMETER, Reply(status=304))
    await env.client().set_operating_mode(FACILITY, OperatingMode.AUTOMATIC)


async def test_set_operating_mode_requires_remote_control(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload=component("2", "0")))
    with pytest.raises(RemoteControlDisabledError):
        await env.client().set_operating_mode(FACILITY, OperatingMode.AUTOMATIC)
    assert env.state.received("PUT", PARAMETER) == []


async def test_expired_token_is_renewed_once(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", FACILITIES, Reply(status=401))
    renewed = token(USER) + "-renewed"
    env.state.add("POST", LOGIN, Reply(payload={"userData": {}}, headers={"Authorization": renewed}))
    env.state.add("GET", FACILITIES, Reply(payload=[]))
    assert await env.client().get_facilities() == []
    assert len(env.state.received("POST", LOGIN)) == 2
    first, retried = env.state.received("GET", FACILITIES)
    assert first.headers["Authorization"] == token()
    assert retried.headers["Authorization"] == renewed
    login_ok(env.state)
    env.state.add("GET", FACILITIES, Reply(status=401))
    login_ok(env.state)
    env.state.add("GET", FACILITIES, Reply(status=401))
    with pytest.raises(AuthenticationError):
        await env.client().get_facilities()


async def test_request_errors(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    login_ok(env.state)
    env.state.add("GET", FACILITIES, Reply(status=502))
    client = env.client()
    with pytest.raises(ConnectionFailedError):
        await client.get_facilities()
    monkeypatch.setattr(cloud, "_TIMEOUT", aiohttp.ClientTimeout(total=0.1))
    env.state.add("GET", FACILITIES, Reply(payload=[], delay=2))
    with pytest.raises(ConnectionFailedError):
        await client.get_facilities()
    env.state.add("GET", FACILITIES, Reply(body="not json", content_type="text/plain"))
    with pytest.raises(ConnectionFailedError):
        await client.get_facilities()
    env.state.add("GET", FACILITIES, Reply(body=b"\xff\xfe[]", content_type="application/json"))
    with pytest.raises(ConnectionFailedError):
        await client.get_facilities()


async def test_not_modified_on_get_is_communication_error(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", FACILITIES, Reply(status=304))
    with pytest.raises(CommunicationError):
        await env.client().get_facilities()


@pytest.mark.parametrize(
    "payload",
    [
        {"facilityId": FACILITY},
        [FACILITY],
        [{"name": "ohne Id"}],
        [{"facilityId": "abc"}],
        [{"facilityId": None}],
        [{"facilityId": FACILITY, "protocol3200Info": "PE1"}],
        [{"facilityId": FACILITY, "protocol3200Info": {"productType": 7}}],
    ],
)
async def test_malformed_facilities(env: Env, payload: Any) -> None:
    login_ok(env.state)
    env.state.add("GET", FACILITIES, Reply(payload=payload))
    with pytest.raises(CommunicationError):
        await env.client().get_facilities()


@pytest.mark.parametrize(
    "components",
    [
        {"type": "BOILER"},
        ["BOILER"],
        [{"type": "BOILER"}],
        [{"type": "BOILER", "componentId": None}],
    ],
)
async def test_malformed_component_list(env: Env, components: Any) -> None:
    login_ok(env.state)
    env.state.add("GET", COMPONENTS, Reply(payload=components))
    with pytest.raises(CommunicationError):
        await env.client().get_boiler_state(FACILITY)


def _with_config(config: Any) -> dict[str, Any]:
    return {"componentId": "1_100", "type": "BOILER", "topView": {"configParams": config}}


@pytest.mark.parametrize(
    "detail",
    [
        ["not", "a", "dict"],
        {"componentId": "1_100", "topView": ["x"]},
        _with_config(["mode2", "remoteOn"]),
        _with_config({"mode2": "2", "remoteOn": {"value": "1"}}),
        _with_config({"mode2": {"value": "2"}, "remoteOn": None}),
    ],
)
async def test_malformed_component_detail(env: Env, detail: Any) -> None:
    login_ok(env.state)
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload=detail))
    with pytest.raises(CommunicationError):
        await env.client().get_boiler_state(FACILITY)


async def test_missing_remote_on_and_empty_top_view(env: Env) -> None:
    login_ok(env.state)
    client = env.client()
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload=_with_config({"mode2": {"id": "1", "value": "2"}})))
    with pytest.raises(CloudParameterNotFoundError, match="remoteOn"):
        await client.get_boiler_state(FACILITY)
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload={"componentId": "1_100", "topView": None}))
    with pytest.raises(CloudParameterNotFoundError, match="mode2"):
        await client.get_boiler_state(FACILITY)


async def test_set_operating_mode_without_parameter_id(env: Env) -> None:
    login_ok(env.state)
    config = component("2", "1")["topView"]["configParams"]
    del config["mode2"]["id"]
    env.state.add("GET", COMPONENTS, Reply(payload=COMPONENT_LIST))
    env.state.add("GET", COMPONENT, Reply(payload=_with_config(config)))
    with pytest.raises(CommunicationError):
        await env.client().set_operating_mode(FACILITY, OperatingMode.AUTOMATIC)
    assert env.state.received("PUT", PARAMETER) == []


# --- Fröling Connect 0.2.0: components, snapshot, writing, schedules ------------------------------------


def fixture(name: str) -> Any:
    return json.loads((CLOUD_FIXTURES / name).read_text())


def detail_path(component_id: str) -> str:
    return f"{FCS}/component/{component_id}"


def parameter_path(parameter_id: str) -> str:
    return f"{FCS}/parameter/{parameter_id}"


def serve_list(state: ServerState) -> None:
    state.add("GET", COMPONENTS, Reply(payload=fixture("component_list.json")))


def serve_detail(state: ServerState, component_id: str, payload: Any = None) -> None:
    data = payload if payload is not None else fixture(f"component_{component_id}.json")
    state.add("GET", detail_path(component_id), Reply(payload=data))


def boiler_with_remote(remote: str) -> dict[str, Any]:
    data: dict[str, Any] = fixture("component_1_100.json")
    data["topView"]["configParams"]["remoteOn"]["value"] = remote
    return data


async def test_facility_equipment_number(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", FACILITIES, Reply(payload=fixture("facilities.json")))
    assert await env.client().get_facilities() == [Facility(FACILITY, "Home", "PE1 Pellet 20", "100000001")]
    login_ok(env.state)
    env.state.add("GET", FACILITIES, Reply(payload=[{"facilityId": FACILITY, "equipmentNumber": 100}]))
    with pytest.raises(CommunicationError):
        await env.client().get_facilities()


async def test_read_endpoints(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3100")
    env.state.add("GET", OVERVIEW, Reply(payload=fixture("overview.json")))
    env.state.add("GET", INFO, Reply(payload=fixture("info.json")))
    env.state.add("GET", HEADER, Reply(payload=fixture("header.json")))
    env.state.add("GET", ERRORS, Reply(payload=fixture("errors.json")))
    client = env.client()
    components = await client.get_components(FACILITY)
    assert [c.key for c in components][:3] == ["boiler", "hc1", "hc2"]
    detail = await client.get_component_detail(FACILITY, "300_3100")
    assert detail.parameters["flowTempAtPlus10"].id == "7_88"
    overview = await client.get_overview(FACILITY)
    assert overview.outside_temperature == "11"
    info = await client.get_system_info(FACILITY)
    assert info.equipment_number == "100000001"
    header = await client.get_header(FACILITY)
    assert header.online
    assert header.last_signal == datetime(2025, 1, 2, 12, 0, 0, 750000, tzinfo=UTC)
    errors = await client.get_errors(FACILITY)
    assert [e.severity for e in errors] == ["alarm", "warning"]


@pytest.mark.parametrize(
    ("headers", "retry_after"),
    [({"Retry-After": "30"}, 30.0), ({}, None), ({"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, None)],
)
async def test_rate_limited(env: Env, headers: dict[str, str], retry_after: float | None) -> None:
    login_ok(env.state)
    env.state.add("GET", OVERVIEW, Reply(status=429, headers=headers))
    with pytest.raises(RateLimitedError) as err:
        await env.client().get_overview(FACILITY)
    assert err.value.retry_after == retry_after


@pytest.mark.parametrize(
    ("header", "retry_after"), [(" 30 ", 30.0), ("30\t", 30.0), (" ", None), ("\u00b2", None)]
)
def test_rate_limited_strips_retry_after(header: str, retry_after: float | None) -> None:
    response = cast(aiohttp.ClientResponse, SimpleNamespace(headers={"Retry-After": header}))
    assert cloud._rate_limited(response).retry_after == retry_after


async def test_rate_limited_on_write(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3100")
    serve_detail(env.state, "1_100")
    env.state.add("PUT", parameter_path("7_88"), Reply(status=429, headers={"Retry-After": "60"}))
    client = env.client()
    with pytest.raises(RateLimitedError) as err:
        await client.set_value(FACILITY, "hc1_curve_flow_plus10", 47)
    assert err.value.retry_after == 60.0
    assert len(env.state.received("PUT", parameter_path("7_88"))) == 1  # never sent twice
    serve_detail(env.state, "300_3100")
    env.state.add("POST", TIME_WINDOWS, Reply(status=429, headers={"Retry-After": "120"}))
    with pytest.raises(RateLimitedError) as err:
        await client.set_schedule(FACILITY, "hc1", {Weekday.MONDAY: []})
    assert err.value.retry_after == 120.0
    assert len(env.state.received("POST", TIME_WINDOWS)) == 1


async def test_rate_limited_login(env: Env) -> None:
    env.state.add("POST", LOGIN, Reply(status=429, headers={"Retry-After": "5"}))
    with pytest.raises(RateLimitedError) as err:
        await env.client().login()
    assert err.value.retry_after == 5.0


async def test_read_snapshot_overview_only(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", OVERVIEW, Reply(payload=fixture("overview.json")))
    snapshot = await env.client().read_snapshot(FACILITY, details=False)
    assert not snapshot.details_loaded
    assert snapshot.value("pellet_counter") == 1142.0
    assert env.state.received("GET", COMPONENTS) == []


async def test_read_snapshot_with_details(env: Env) -> None:
    login_ok(env.state)
    env.state.add("GET", OVERVIEW, Reply(payload=fixture("overview.json")))
    components = [
        *fixture("component_list.json"),
        {"componentId": "9_9", "componentNumber": 1, "type": "SOLAR"},
    ]
    env.state.add("GET", COMPONENTS, Reply(payload=components))
    for component in components[:-1]:
        if component["componentId"] != "300_3110":
            serve_detail(env.state, component["componentId"])
    client = env.client()
    snapshot = await client.read_snapshot(FACILITY, details=True, skip={"hc2"})
    assert snapshot.details_loaded
    assert snapshot.value("hc1_curve_flow_plus10") == 46.0
    assert snapshot.value("hc2_curve_flow_plus10") is None
    assert snapshot.value("hc2_flow_temperature") == 28.0  # from the overview
    assert env.state.received("GET", detail_path("300_3110")) == []
    assert env.state.received("GET", detail_path("9_9")) == []
    # The details are cached for writing: no further GET of the component.
    env.state.add("PUT", parameter_path("7_88"), Reply(status=304))
    await client.set_value(FACILITY, "hc1_curve_flow_plus10", 46)
    assert len(env.state.received("GET", detail_path("300_3100"))) == 1
    assert len(env.state.received("GET", COMPONENTS)) == 1


async def test_read_snapshot_skips_broken_details(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    login_ok(env.state)
    env.state.add("GET", OVERVIEW, Reply(payload=fixture("overview.json")))
    serve_list(env.state)
    for component in fixture("component_list.json"):
        component_id = component["componentId"]
        if component_id == "300_3110":
            serve_detail(env.state, component_id, ["not", "an", "object"])
        elif component_id == "200_2100":
            env.state.add("GET", detail_path(component_id), Reply(status=404))
        else:
            serve_detail(env.state, component_id)
    with caplog.at_level(logging.WARNING, logger="froeling_lambdatronic.cloud"):
        snapshot = await env.client().read_snapshot(FACILITY, details=True)
    assert snapshot.details_loaded
    assert snapshot.value("hc1_curve_flow_plus10") == 46.0
    assert snapshot.value("hc2_curve_flow_plus10") is None
    assert snapshot.value("hc2_flow_temperature") == 28.0  # from the overview
    assert snapshot.value("dhw1_recharge_below") is None
    assert snapshot.value("circulation_pump_off_above") is not None
    messages = [r.getMessage() for r in caplog.records if r.name == "froeling_lambdatronic.cloud"]
    assert len(messages) == 2
    assert "300_3110" in messages[0]
    assert "200_2100" in messages[1]
    assert all(str(USER) not in m and str(FACILITY) not in m for m in messages)


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply(status=429), RateLimitedError),
        (Reply(status=500), ConnectionFailedError),
        (Reply(status=401), AuthenticationError),
    ],
)
async def test_read_snapshot_aborts_on_transport_errors(
    env: Env, reply: Reply, error: type[Exception]
) -> None:
    login_ok(env.state)
    env.state.add("GET", OVERVIEW, Reply(payload=fixture("overview.json")))
    serve_list(env.state)
    env.state.add("GET", detail_path("1_100"), reply)
    if reply.status == 401:
        login_ok(env.state)
        env.state.add("GET", detail_path("1_100"), reply)
    with pytest.raises(error):
        await env.client().read_snapshot(FACILITY, details=True)
    assert env.state.received("GET", detail_path("300_3100")) == []


async def test_read_snapshot_aborts_on_timeout(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cloud, "_TIMEOUT", aiohttp.ClientTimeout(total=0.05))
    login_ok(env.state)
    env.state.add("GET", OVERVIEW, Reply(payload=fixture("overview.json")))
    serve_list(env.state)
    env.state.add("GET", detail_path("1_100"), Reply(payload={}, delay=0.5))
    with pytest.raises(ConnectionFailedError):
        await env.client().read_snapshot(FACILITY, details=True)
    assert env.state.received("GET", detail_path("300_3100")) == []


@pytest.mark.parametrize(
    ("key", "value", "target", "sent"),
    [
        ("hc1_curve_flow_plus10", 47, ("300_3100", "7_88"), "47"),
        ("hc2_heating_limit", -2.5, ("300_3110", "7_94"), "-2.5"),
        ("hc1_operating_mode", 3, ("300_3100", "9_9989"), "3"),
        ("pellet_stock", 4.5, ("1_906", "7_1121"), "4.5"),
        ("pellet_filling_start_2", time(21, 30), ("1_906", "10_516"), "21:30"),
        ("auto_feeder_off", True, ("1_906", "8_973"), "1"),
        ("reset_pellet_counter", True, ("1_906", "8_1125"), "1"),
        ("dhw1_mode", 2, ("200_2100", "9_9971"), "2"),
        ("circulation_pump_off_above", 40.0, ("1_570", "7_752"), "40"),
        ("buffer1_circuit_release", 35, ("400_4100", "7_443"), "35"),
    ],
)
async def test_set_value(env: Env, key: str, value: float | time, target: tuple[str, str], sent: str) -> None:
    component_id, parameter_id = target
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, component_id)
    if component_id != "1_100":
        serve_detail(env.state, "1_100")
    env.state.add("PUT", parameter_path(parameter_id), Reply(payload={}))
    await env.client().set_value(FACILITY, key, value)
    assert env.state.received("PUT", parameter_path(parameter_id))[0].json == {"value": sent}


async def test_set_value_on_boiler_and_inverted_bool(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "1_100")
    env.state.add("PUT", parameter_path("995_9887"), Reply(status=304))
    env.state.add("PUT", parameter_path("7_480"), Reply(payload={}))
    client = env.client()
    await client.set_value(FACILITY, "boiler_on", False)
    await client.set_value(FACILITY, "boiler_setpoint", 72.5)
    assert env.state.received("PUT", parameter_path("995_9887"))[0].json == {"value": "1"}
    assert env.state.received("PUT", parameter_path("7_480"))[0].json == {"value": "72.5"}
    assert len(env.state.received("GET", detail_path("1_100"))) == 1


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("hc1_curve_flow_plus10", 111, ValueOutOfRangeError),
        ("hc1_curve_flow_plus10", float("nan"), ValueOutOfRangeError),
        ("hc1_operating_mode", 6, ValueOutOfRangeError),
        ("hc1_flow_temperature", 40, WriteRejectedError),
        ("hc1_curve_flow_plus10", time(10, 0), TypeError),
        ("hc1_pump", True, WriteRejectedError),
        ("hc1_operating_mode", 1.5, TypeError),
    ],
)
async def test_set_value_is_validated_before_sending(
    env: Env, key: str, value: float | time, error: type[Exception]
) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3100")
    serve_detail(env.state, "1_100")
    with pytest.raises(error):
        await env.client().set_value(FACILITY, key, value)
    assert [r for r in env.state.requests if r.method == "PUT"] == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("pellet_filling_start_1", 600),
        ("auto_feeder_off", 1),
        ("pellet_stock", True),
    ],
)
async def test_set_value_type_checks_on_feed(env: Env, key: str, value: float) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "1_906")
    with pytest.raises(TypeError):
        await env.client().set_value(FACILITY, key, value)


@pytest.mark.parametrize("value", [time(21, 30, 15), time(21, 30, 0, 1)])
async def test_set_value_time_with_seconds_is_rejected(env: Env, value: time) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "1_906")
    with pytest.raises(ValueError, match="whole minutes"):
        await env.client().set_value(FACILITY, "pellet_filling_start_2", value)
    assert [r for r in env.state.requests if r.method == "PUT"] == []


async def test_set_value_out_of_range_reports_library_units(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "1_906")
    with pytest.raises(ValueOutOfRangeError) as err:
        await env.client().set_value(FACILITY, "pellet_stock", 100.5)
    assert (err.value.minimum, err.value.maximum) == (0.0, 100.0)


async def test_set_value_unknown_key(env: Env) -> None:
    with pytest.raises(ValueError, match="no Fröling Connect parameter"):
        await env.client().set_value(FACILITY, "hc1_room_setpoint_heating", 20)
    assert env.state.requests == []


async def test_set_value_buffer2_ignition_is_unknown(env: Env) -> None:
    with pytest.raises(ValueError, match="no Fröling Connect parameter"):
        await env.client().set_value(FACILITY, "buffer2_ignition_below_buffer_temperature", 10)
    assert env.state.requests == []


async def test_set_value_missing_component_and_parameter(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_list(env.state)
    client = env.client()
    with pytest.raises(CloudParameterNotFoundError, match="hc3"):
        await client.set_value(FACILITY, "hc3_curve_flow_plus10", 40)
    assert len(env.state.received("GET", COMPONENTS)) == 1
    with pytest.raises(CloudParameterNotFoundError, match="hc3"):
        await client.set_value(FACILITY, "hc3_curve_flow_plus10", 40)
    assert len(env.state.received("GET", COMPONENTS)) == 2  # the cached list is refreshed once
    detail = fixture("component_300_3100.json")
    detail["setupView"] = [p for p in detail["setupView"] if p["name"] != "frostProtectionTemp"]
    serve_detail(env.state, "300_3100", detail)
    with pytest.raises(CloudParameterNotFoundError, match="frostProtectionTemp"):
        await client.set_value(FACILITY, "hc1_frost_protection", 5)


async def test_set_value_requires_remote_control(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3100")
    serve_detail(env.state, "1_100", boiler_with_remote("0"))
    client = env.client()
    with pytest.raises(RemoteControlDisabledError):
        await client.set_value(FACILITY, "hc1_curve_flow_plus10", 47)
    assert len(env.state.received("GET", detail_path("1_100"))) == 1
    # A cached "disabled" is re-read once before giving up.
    serve_detail(env.state, "1_100", boiler_with_remote("1"))
    env.state.add("PUT", parameter_path("7_88"), Reply(payload={}))
    await client.set_value(FACILITY, "hc1_curve_flow_plus10", 47)
    assert len(env.state.received("GET", detail_path("1_100"))) == 2
    boiler = fixture("component_1_100.json")
    del boiler["topView"]["configParams"]["remoteOn"]
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "1_100", boiler)
    with pytest.raises(CloudParameterNotFoundError, match="remoteOn"):
        await env.client().set_value(FACILITY, "boiler_setpoint", 70)


async def test_set_value_without_boiler_component(env: Env) -> None:
    login_ok(env.state)
    components = [c for c in fixture("component_list.json") if c["type"] != "BOILER"]
    env.state.add("GET", COMPONENTS, Reply(payload=components))
    serve_detail(env.state, "1_906")
    with pytest.raises(CloudParameterNotFoundError, match="boiler"):
        await env.client().set_value(FACILITY, "pellet_stock", 4)


async def test_set_value_refreshes_stale_parameter_id(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    stale = fixture("component_300_3100.json")
    for parameter in stale["setupView"]:
        if parameter["name"] == "flowTempAtPlus10":
            parameter["id"] = "7_old"
    serve_detail(env.state, "300_3100", stale)
    serve_detail(env.state, "1_100")
    env.state.add("PUT", parameter_path("7_old"), Reply(status=404))
    serve_detail(env.state, "300_3100")
    env.state.add("PUT", parameter_path("7_88"), Reply(payload={}))
    await env.client().set_value(FACILITY, "hc1_curve_flow_plus10", 47)
    assert env.state.received("PUT", parameter_path("7_88"))[0].json == {"value": "47"}
    # A second 404 is reported.
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3100")
    serve_detail(env.state, "1_100")
    env.state.add("PUT", parameter_path("7_88"), Reply(status=404))
    serve_detail(env.state, "300_3100")
    env.state.add("PUT", parameter_path("7_88"), Reply(status=404))
    client = env.client()
    with pytest.raises(ConnectionFailedError):
        await client.set_value(FACILITY, "hc1_curve_flow_plus10", 47)
    # The caches are dropped: the next call reloads the component list and the detail.
    serve_list(env.state)
    serve_detail(env.state, "300_3100")
    env.state.add("PUT", parameter_path("7_88"), Reply(payload={}))
    await client.set_value(FACILITY, "hc1_curve_flow_plus10", 47)
    assert len(env.state.received("GET", COMPONENTS)) == 3
    assert len(env.state.received("GET", detail_path("300_3100"))) == 5


async def test_stale_component_id_drops_the_cached_list(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3100")
    client = env.client()
    await client.get_components(FACILITY)
    await client.get_component_detail(FACILITY, "300_3100")
    env.state.add("GET", detail_path("300_3100"), Reply(status=404))
    with pytest.raises(ConnectionFailedError):
        await client.get_schedule(FACILITY, "hc1")
    serve_list(env.state)
    serve_detail(env.state, "300_3100")
    await client.get_schedule(FACILITY, "hc1")
    assert len(env.state.received("GET", COMPONENTS)) == 2


async def test_get_schedule(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "1_570")
    schedule = await env.client().get_schedule(FACILITY, "circulation")
    assert schedule[Weekday.SUNDAY] == (Phase(270, 540), Phase(660, 780), Phase(960, 1320))
    login_ok(env.state)
    serve_list(env.state)
    with pytest.raises(CloudParameterNotFoundError, match="hc5"):
        await env.client().get_schedule(FACILITY, "hc5")


async def test_set_schedule(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3110")
    serve_detail(env.state, "1_100")
    env.state.add("POST", TIME_WINDOWS, Reply(payload={"updatedTimeWindowIds": [63, 69]}))
    changes = {
        Weekday.MONDAY: [Phase(330, 480), Phase(960, 1440)],
        Weekday.SUNDAY: [],
    }
    await env.client().set_schedule(FACILITY, "hc2", changes)
    (post,) = env.state.received("POST", TIME_WINDOWS)
    assert [day["weekDay"] for day in post.json] == [d.value for d in Weekday]
    assert [day["id"] for day in post.json] == list(range(63, 70))
    assert post.json[0]["phases"] == [
        {"startHour": 5, "startMinute": 30, "endHour": 8, "endMinute": 0},
        {"startHour": 16, "startMinute": 0, "endHour": 24, "endMinute": 0},
    ]
    assert post.json[1]["phases"] == [{"startHour": 6, "startMinute": 0, "endHour": 22, "endMinute": 0}]
    assert post.json[6]["phases"] == []


def _with_broken_monday_phase(component_id: str) -> dict[str, Any]:
    data: dict[str, Any] = fixture(f"component_{component_id}.json")
    monday = next(day for day in data["timeWindowsView"] if day["weekDay"] == "MONDAY")
    monday["phases"].append({"startHour": "x"})
    return data


async def test_set_schedule_refuses_to_drop_unreadable_phases(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3110", _with_broken_monday_phase("300_3110"))
    with pytest.raises(CommunicationError, match="MONDAY"):
        await env.client().set_schedule(FACILITY, "hc2", {Weekday.TUESDAY: [Phase(360, 480)]})
    assert env.state.received("POST", TIME_WINDOWS) == []


async def test_set_schedule_may_replace_the_day_with_unreadable_phases(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3110", _with_broken_monday_phase("300_3110"))
    serve_detail(env.state, "1_100")
    env.state.add("POST", TIME_WINDOWS, Reply(payload={}))
    await env.client().set_schedule(FACILITY, "hc2", {Weekday.MONDAY: [Phase(360, 480)]})
    (post,) = env.state.received("POST", TIME_WINDOWS)
    assert post.json[0]["phases"] == [{"startHour": 6, "startMinute": 0, "endHour": 8, "endMinute": 0}]


async def test_set_schedule_not_modified(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "400_4100")
    serve_detail(env.state, "1_100")
    env.state.add("POST", TIME_WINDOWS, Reply(status=304))
    await env.client().set_schedule(FACILITY, "buffer1", {Weekday.MONDAY: [Phase(0, 1440)]})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({Weekday.MONDAY: [Phase(i * 60, i * 60 + 30) for i in range(5)]}, "at most 4"),
        ({Weekday.MONDAY: [Phase(0, 120), Phase(60, 180)]}, "overlap"),
    ],
)
async def test_set_schedule_is_validated_first(
    env: Env, changes: dict[Weekday, list[Phase]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        await env.client().set_schedule(FACILITY, "hc1", changes)
    assert env.state.requests == []


async def test_set_schedule_requires_remote_control_and_schedule(env: Env) -> None:
    login_ok(env.state)
    serve_list(env.state)
    serve_detail(env.state, "300_3100")
    serve_detail(env.state, "1_100", boiler_with_remote("0"))
    client = env.client()
    with pytest.raises(RemoteControlDisabledError):
        await client.set_schedule(FACILITY, "hc1", {Weekday.MONDAY: []})
    serve_detail(env.state, "1_906")
    with pytest.raises(CloudParameterNotFoundError, match="schedule"):
        await client.set_schedule(FACILITY, "feed", {Weekday.MONDAY: []})
    assert env.state.received("POST", TIME_WINDOWS) == []
