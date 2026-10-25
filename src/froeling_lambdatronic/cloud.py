"""Minimal Fröling Connect client: switch the system operating mode (parameter 'mode2')."""

import base64
import binascii
import json
from dataclasses import dataclass
from enum import IntEnum
from http import HTTPStatus
from typing import Any

import aiohttp

from .exceptions import (
    AuthenticationError,
    CloudParameterNotFoundError,
    CommunicationError,
    ConnectionFailedError,
    RemoteControlDisabledError,
)

DEFAULT_BASE_URL = "https://connect-api.froeling.com"
API_BASE = DEFAULT_BASE_URL
_LOGIN = "/connect/v1.0/resources/login"
_FACILITIES = "/connect/v1.0/resources/service/user/{user_id}/facility"
_FCS = "/fcs/v1.0/resources/user/{user_id}/facility/{facility_id}"
_TIMEOUT = aiohttp.ClientTimeout(total=20)


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
                if response.status != HTTPStatus.OK:
                    raise ConnectionFailedError(f"login failed with HTTP {response.status}")
                token = response.headers.get("Authorization", "")
        except (aiohttp.ClientError, TimeoutError) as err:
            raise ConnectionFailedError(f"Fröling Connect not reachable: {err}") from err
        if not token:
            raise CommunicationError("login response carries no token")
        self._user_id = _user_id_from_token(token)
        self._token = token

    async def get_facilities(self) -> list[Facility]:
        """List all facilities of the account."""
        data = await self._request("GET", _FACILITIES)
        if not isinstance(data, list):
            raise CommunicationError("facility list is not a list")
        return [_parse_facility(item) for item in data]

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
        json_body: dict[str, str] | None = None,
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
            except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as err:
                raise ConnectionFailedError(f"Fröling Connect request failed: {err}") from err
            # 401: renew the token once, outside the closed response context.
            if attempt == 0:
                await self.login()
        raise AuthenticationError("token renewal did not help")

    @staticmethod
    async def _handle_response(method: str, url: str, response: aiohttp.ClientResponse) -> Any:  # noqa: ANN401 - JSON payloads are untyped
        if response.status == HTTPStatus.NOT_MODIFIED:
            # Fröling Connect answers an unchanged write with 304; a read must carry data.
            if method == "PUT":
                return None
            raise CommunicationError(f"{method} {url} answered 304 without data")
        if not HTTPStatus.OK <= response.status < HTTPStatus.MULTIPLE_CHOICES:
            raise ConnectionFailedError(f"{method} {url} failed with HTTP {response.status}")
        return await response.json(content_type=None)


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
    return Facility(facility_id=facility_id, name=str(item.get("name", "")), product_type=product_type)


def _user_id_from_token(token: str) -> int:
    try:
        payload = token.split(".")[1]
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return int(data["userId"])
    except (IndexError, KeyError, ValueError, binascii.Error, TypeError) as err:
        raise CommunicationError("unexpected token format") from err
