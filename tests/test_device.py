"""Tests for LambdatronicClient against the fake server with real register values."""

import socket

import pytest

from froeling_lambdatronic.device import LambdatronicClient
from froeling_lambdatronic.exceptions import (
    CommunicationError,
    ConnectionFailedError,
    NoResponseError,
    ProtocolNotEnabledError,
    ReadOnlyRegisterError,
    ValueOutOfRangeError,
    WriteRejectedError,
)
from froeling_lambdatronic.models import Equipment
from froeling_lambdatronic.registers import BOILER, RegisterDef, heating_circuit, hot_water
from tests.conftest import load_snapshot
from tests.fake_server import FakeLambdatronic, split_snapshot

EQUIPMENT = Equipment(heating_circuits=(1, 2), hot_water=(1,), buffers=(1,))


def client_for(address: tuple[str, int], slave: int = 2) -> LambdatronicClient:
    return LambdatronicClient(address[0], address[1], slave, timeout=0.2, request_pause=0.0)


async def test_probe_returns_system_state(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        assert await client.probe() == 2


async def test_detect_equipment(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        assert await client.detect_equipment() == EQUIPMENT


async def test_read_values(fake_address: tuple[str, int], snapshot: dict[str, int]) -> None:
    async with client_for(fake_address) as client:
        values = await client.read_values(EQUIPMENT)
    assert values.boiler_temperature == snapshot["30001"] / 2
    assert values.system_state == 2
    assert values.boiler_state == snapshot["34002"]
    assert values.errors == ()
    assert values.pellet_consumption_total == pytest.approx(42.9)
    assert values.boiler_demand is (snapshot["30057"] == 1)
    assert set(values.heating_circuits) == {1, 2}
    assert values.heating_circuits[1].flow_temperature == snapshot["31031"] / 2
    assert values.hot_water[1].temperature == snapshot["31631"] / 2
    assert values.buffers[1].middle is None  # 254 = unplugged probe
    assert values.raw[30001] == snapshot["30001"]


async def test_read_values_with_active_errors(snapshot: dict[str, int]) -> None:
    inputs, holdings = split_snapshot(snapshot)
    inputs[3000], inputs[3001] = 11, 24
    server = FakeLambdatronic(inputs, holdings)
    address = await server.start()
    try:
        async with client_for(address) as client:
            values = await client.read_values(EQUIPMENT)
        assert values.errors == (11, 24)
        assert values.fault_active
    finally:
        await server.stop()


async def test_read_parameters(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        parameters = await client.read_parameters(EQUIPMENT)
    assert parameters.boiler_setpoint == 70.0
    assert parameters.pellet_stock == pytest.approx(4.5)
    hc1 = parameters.heating_circuits[1]
    assert (hc1.curve_flow_plus10, hc1.curve_flow_minus10, hc1.setback) == (46.0, 63.0, 15.0)
    assert (hc1.max_flow, hc1.heating_limit, hc1.heating_limit_setback) == (75.0, 20.0, 7.0)
    assert hc1.operating_mode == 1
    assert parameters.heating_circuits[2].curve_flow_plus10 == 29.0
    assert parameters.hot_water[1].setpoint == 55.0
    assert parameters.hot_water[1].recharge_below == 45.0


async def test_write_and_read_back(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        assert await client.write(heating_circuit(2).heating_limit_setback, 7.5) == 7.5
        assert await client.write(heating_circuit(1).operating_mode, 0) == 0
    assert fake.holding_registers[1067] == 15
    assert fake.holding_registers[8046] == 0


async def test_write_validation_happens_before_sending(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    async with client_for(fake_address) as client:
        before = len(fake.requests)
        with pytest.raises(ValueOutOfRangeError):
            await client.write(BOILER.boiler_setpoint, 95)
        with pytest.raises(ReadOnlyRegisterError):
            await client.write(BOILER.boiler_temperature, 60)
        assert len(fake.requests) == before


async def test_write_errors_are_mapped(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        fake.illegal_function_on_write = True
        with pytest.raises(ProtocolNotEnabledError):
            await client.write(BOILER.boiler_setpoint, 72)
        fake.illegal_function_on_write = False
        fake.reject_write.add(BOILER.boiler_setpoint.address)
        with pytest.raises(WriteRejectedError) as err:
            await client.write(BOILER.boiler_setpoint, 72)
        assert not isinstance(err.value, ProtocolNotEnabledError)
        fake.reject_write.clear()
        fake.ignore_write.add(hot_water(1).setpoint.address)
        with pytest.raises(WriteRejectedError, match="read back"):
            await client.write(hot_water(1).setpoint, 50)


async def test_wrong_slave_is_no_response(fake_address: tuple[str, int]) -> None:
    client = LambdatronicClient(fake_address[0], fake_address[1], 7, timeout=0.05, request_pause=0.0)
    async with client:
        with pytest.raises(NoResponseError):
            await client.probe()


async def test_unreachable_gateway() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = LambdatronicClient("127.0.0.1", port, timeout=0.1)
    with pytest.raises(ConnectionFailedError):
        await client.connect()
    with pytest.raises(ConnectionFailedError):
        await client.probe()
    await client.close()


async def test_illegal_address_is_communication_error(fake_address: tuple[str, int]) -> None:
    outside_documented_range = RegisterDef(key="undocumented", number=35001)
    async with client_for(fake_address) as client:
        with pytest.raises(CommunicationError):
            await client._read_raw([outside_documented_range])


async def test_brauchwasser_snapshot_state() -> None:
    inputs, holdings = split_snapshot(load_snapshot("snapshot_brauchwasser.json"))
    server = FakeLambdatronic(inputs, holdings)
    address = await server.start()
    try:
        async with client_for(address) as client:
            assert await client.probe() == 1
            values = await client.read_values(EQUIPMENT)
        assert values.hot_water[1].pump == 100.0
    finally:
        await server.stop()
