"""Exceptions raised by the Modbus TCP client."""


class ModbusError(Exception):
    """Base class for all Modbus errors."""


class ModbusConnectionError(ModbusError):
    """The TCP connection could not be established or was lost."""


class ModbusTimeoutError(ModbusError):
    """No matching response arrived in time."""


class ModbusResponseError(ModbusError):
    """The response was malformed (function code, length, unit id or echo)."""


class ModbusExceptionResponse(ModbusError):
    """The device answered with a Modbus exception response."""

    exception_code: int = 0

    def __init__(self, function_code: int, exception_code: int | None = None) -> None:
        if exception_code is not None:
            self.exception_code = exception_code
        self.function_code = function_code
        super().__init__(f"Modbus exception {self.exception_code:#04x} for function {function_code:#04x}")


class IllegalFunctionError(ModbusExceptionResponse):
    """Exception 0x01: function not supported (Fröling: writing without protocol 2014)."""

    exception_code = 0x01


class IllegalAddressError(ModbusExceptionResponse):
    """Exception 0x02: address outside the valid range."""

    exception_code = 0x02


class IllegalValueError(ModbusExceptionResponse):
    """Exception 0x03: illegal data value (e.g. too many registers requested)."""

    exception_code = 0x03


class DeviceFailureError(ModbusExceptionResponse):
    """Exception 0x04: device failure (Fröling: value outside the allowed range)."""

    exception_code = 0x04


class DeviceBusyError(ModbusExceptionResponse):
    """Exception 0x06: device busy, the request may be retried."""

    exception_code = 0x06


_BY_CODE: dict[int, type[ModbusExceptionResponse]] = {
    cls.exception_code: cls
    for cls in (
        IllegalFunctionError,
        IllegalAddressError,
        IllegalValueError,
        DeviceFailureError,
        DeviceBusyError,
    )
}


def exception_from_response(function_code: int, exception_code: int) -> ModbusExceptionResponse:
    """Build the exception matching a Modbus exception response."""
    cls = _BY_CODE.get(exception_code)
    if cls is None:
        return ModbusExceptionResponse(function_code, exception_code)
    return cls(function_code)
