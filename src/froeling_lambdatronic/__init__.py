"""Async client for Fröling Lambdatronic 3200 boilers."""

from .cloud import CloudBoilerState, Facility, FroelingConnectClient, OperatingMode
from .device import LambdatronicClient
from .exceptions import (
    AuthenticationError,
    CloudParameterNotFoundError,
    CommunicationError,
    ConnectionFailedError,
    FroelingError,
    NoResponseError,
    ProtocolNotEnabledError,
    ReadOnlyRegisterError,
    RemoteControlDisabledError,
    ValueOutOfRangeError,
    WriteRejectedError,
)
from .models import (
    BufferValues,
    Equipment,
    HeatingCircuitParameters,
    HeatingCircuitValues,
    HotWaterParameters,
    HotWaterValues,
    LambdatronicParameters,
    LambdatronicValues,
)
from .registers import BOILER, ERROR_SLOTS, RegisterDef, buffer, heating_circuit, hot_water
from .texts import HEATING_CIRCUIT_MODES, boiler_state_text, error_text, system_state_text

__version__ = "0.1.0"

__all__ = [
    "BOILER",
    "ERROR_SLOTS",
    "HEATING_CIRCUIT_MODES",
    "AuthenticationError",
    "BufferValues",
    "CloudBoilerState",
    "CloudParameterNotFoundError",
    "CommunicationError",
    "ConnectionFailedError",
    "Equipment",
    "Facility",
    "FroelingConnectClient",
    "FroelingError",
    "HeatingCircuitParameters",
    "HeatingCircuitValues",
    "HotWaterParameters",
    "HotWaterValues",
    "LambdatronicClient",
    "LambdatronicParameters",
    "LambdatronicValues",
    "NoResponseError",
    "OperatingMode",
    "ProtocolNotEnabledError",
    "ReadOnlyRegisterError",
    "RegisterDef",
    "RemoteControlDisabledError",
    "ValueOutOfRangeError",
    "WriteRejectedError",
    "boiler_state_text",
    "buffer",
    "error_text",
    "heating_circuit",
    "hot_water",
    "system_state_text",
]
