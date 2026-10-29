"""Exceptions raised by froeling_lambdatronic."""


class FroelingError(Exception):
    """Base class for all errors of this library.

    Errors of the Fröling Connect client carry no user or facility id in `str(err)`; they name the
    URL template instead. `__cause__` can hold the raw aiohttp exception with the full URL
    (including both ids): do not log or display `__cause__`, only `str(err)`.
    """


class ConnectionFailedError(FroelingError):
    """The gateway is not reachable or the connection broke."""


class NoResponseError(ConnectionFailedError):
    """The gateway is reachable but the boiler does not answer (wrong slave id, cabling)."""


class CommunicationError(FroelingError):
    """The boiler answered with something unexpected."""


class AuthenticationError(FroelingError):
    """Fröling Connect rejected the credentials."""


class WriteRejectedError(FroelingError):
    """The boiler did not accept a written value."""

    def __init__(self, key: str, reason: str) -> None:
        self.key = key
        self.reason = reason
        super().__init__(f"{key}: {reason}")


class ProtocolNotEnabledError(WriteRejectedError):
    """Writing failed with Illegal Function: 'MODBUS-Protokoll 2014' is disabled on the boiler."""


class ValueOutOfRangeError(FroelingError):
    """A value is outside the allowed range of a register."""

    def __init__(self, key: str, value: float, minimum: float | None, maximum: float | None) -> None:
        self.key = key
        self.value = value
        self.minimum = minimum
        self.maximum = maximum
        super().__init__(f"{key}: {value} is outside {minimum}..{maximum}")


class ReadOnlyRegisterError(FroelingError):
    """A write was attempted on a read-only register."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(f"{key} is read-only")


class RemoteControlDisabledError(FroelingError):
    """'Fernschalten über connect möglich' is disabled for the facility."""


class CloudParameterNotFoundError(FroelingError):
    """The expected parameter was not found in the Fröling Connect response."""


class RateLimitedError(FroelingError):
    """Fröling Connect answered HTTP 429 (too many requests)."""

    def __init__(self, retry_after: float | None) -> None:
        self.retry_after = retry_after
        hint = "" if retry_after is None else f", retry after {retry_after:g} s"
        super().__init__(f"Fröling Connect rate limit reached{hint}")
