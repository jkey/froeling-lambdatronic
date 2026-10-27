"""Package metadata tests."""

import inspect
from importlib.metadata import version

import froeling_lambdatronic


def test_version_matches_metadata() -> None:
    assert froeling_lambdatronic.__version__ == version("froeling-lambdatronic") == "0.2.0b1"


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
        "SYSTEM_STATES",
        "BOILER_STATES",
        "HeatingCircuitRegisters",
        "HotWaterRegisters",
        "BufferRegisters",
        # 0.2.0: Modbus additions
        "CIRCULATION",
        "CirculationRegisters",
        "register_for_key",
        "CirculationValues",
        "BufferParameters",
        "FlatValue",
        "minutes_to_time",
        "time_to_minutes",
        # 0.2.0: Fröling Connect models, catalogue and errors
        "ComponentType",
        "Weekday",
        "Phase",
        "MAX_PHASES_PER_DAY",
        "validate_day",
        "component_key",
        "CloudComponent",
        "CloudParameter",
        "CloudComponentDetail",
        "CloudOverview",
        "CloudOverviewComponent",
        "CloudSystemInfo",
        "CloudHeader",
        "CloudError",
        "CLOUD_REFS",
        "CloudRef",
        "CloudKind",
        "CloudSnapshot",
        "cloud_ref",
        "build_snapshot",
        "equipment_from_components",
        "merge_equipment",
        "RateLimitedError",
    }
    assert set(froeling_lambdatronic.__all__) == expected
    assert len(froeling_lambdatronic.__all__) == len(expected)
    for name in expected:
        assert hasattr(froeling_lambdatronic, name)


def test_all_lists_every_public_export() -> None:
    public = {
        name
        for name, item in vars(froeling_lambdatronic).items()
        if not name.startswith("_") and not inspect.ismodule(item)
    }
    assert public == set(froeling_lambdatronic.__all__)
