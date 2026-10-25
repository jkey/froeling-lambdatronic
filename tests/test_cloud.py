"""Tests for the Fröling Connect client against a real local aiohttp server."""

import asyncio
import base64
import json
import socket
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

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
from froeling_lambdatronic.exceptions import (
    AuthenticationError,
    CloudParameterNotFoundError,
    CommunicationError,
    ConnectionFailedError,
    RemoteControlDisabledError,
)

USER = 12345
FACILITY = 777
LOGIN = "/connect/v1.0/resources/login"
FACILITIES = f"/connect/v1.0/resources/service/user/{USER}/facility"
COMPONENTS = f"/fcs/v1.0/resources/user/{USER}/facility/{FACILITY}/componentList"
COMPONENT = f"/fcs/v1.0/resources/user/{USER}/facility/{FACILITY}/component/1_100"
PARAMETER = f"/fcs/v1.0/resources/user/{USER}/facility/{FACILITY}/parameter/100011_9963"


@dataclass
class Reply:
    status: int = 200
    payload: Any = None
    body: str | None = None
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
            return web.Response(
                status=reply.status, text=reply.body, content_type=reply.content_type, headers=reply.headers
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
