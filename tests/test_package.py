"""Package metadata tests."""

from importlib.metadata import version

import froeling_lambdatronic


def test_version_matches_metadata() -> None:
    assert froeling_lambdatronic.__version__ == version("froeling-lambdatronic")


def test_public_api() -> None:
    expected = {
        "LambdatronicClient",
        "FroelingConnectClient",
        "OperatingMode",
        "Facility",
        "CloudBoilerState",
        "Equipment",
        "LambdatronicValues",
        "LambdatronicParameters",
        "HeatingCircuitValues",
        "HeatingCircuitParameters",
        "HotWaterValues",
        "HotWaterParameters",
        "BufferValues",
        "BOILER",
        "ERROR_SLOTS",
        "RegisterDef",
        "heating_circuit",
        "hot_water",
        "buffer",
        "FroelingError",
        "ConnectionFailedError",
        "NoResponseError",
        "CommunicationError",
        "AuthenticationError",
        "WriteRejectedError",
        "ProtocolNotEnabledError",
        "ValueOutOfRangeError",
        "ReadOnlyRegisterError",
        "RemoteControlDisabledError",
        "CloudParameterNotFoundError",
        "error_text",
        "system_state_text",
        "boiler_state_text",
        "HEATING_CIRCUIT_MODES",
    }
    assert expected <= set(froeling_lambdatronic.__all__)
    for name in expected:
        assert hasattr(froeling_lambdatronic, name)
