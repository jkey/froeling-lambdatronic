"""Tests for LambdatronicClient against the fake server with real register values."""

import asyncio
import socket
from datetime import time

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
from froeling_lambdatronic.models import BufferParameters, CirculationValues, Equipment
from froeling_lambdatronic.registers import (
    BOILER,
    CIRCULATION,
    RegisterDef,
    buffer,
    heating_circuit,
    hot_water,
)
from tests.conftest import load_snapshot
from tests.fake_server import FakeLambdatronic, split_snapshot

EQUIPMENT = Equipment(heating_circuits=(1, 2), hot_water=(1,), buffers=(1,), circulation_pump=True)


def client_for(address: tuple[str, int], slave: int = 2) -> LambdatronicClient:
    return LambdatronicClient(address[0], address[1], slave, timeout=0.2, request_pause=0.0)


async def test_probe_returns_system_state(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        assert await client.probe() == 2


async def test_detect_equipment(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        assert await client.detect_equipment() == EQUIPMENT


async def test_detect_equipment_without_circulation_pump(snapshot: dict[str, int]) -> None:
    inputs, holdings = split_snapshot(snapshot)
    holdings[CIRCULATION.pump_off_above.address] = 0xFFFF
    server = FakeLambdatronic(inputs, holdings)
    address = await server.start()
    try:
        async with client_for(address) as client:
            equipment = await client.detect_equipment()
        assert not equipment.circulation_pump
        assert equipment.heating_circuits == (1, 2)
    finally:
        await server.stop()


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
    assert values.boiler_control_output == snapshot["30015"]
    assert values.hours_since_maintenance == 17702
    assert values.pellet_counter == 1000 * snapshot["30083"] + snapshot["30082"] == 1094
    # 40603 = 0: no return sensor at the circulation line.
    assert values.circulation == CirculationValues(flow_switch=False, pump=0.0, return_temperature=None)
    assert values.flat()["circulation_pump"] == 0.0


async def test_read_values_reads_only_input_registers_of_buffers(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    async with client_for(fake_address) as client:
        await client.read_values(Equipment((), (), (1,)))
    holding_reads = [r for r in fake.requests if r[1] == 3]
    assert not any(
        address <= buffer(1).circuit_release.address < address + count
        for _, _, address, count in holding_reads
    )


async def test_read_values_circulation_with_return_sensor(snapshot: dict[str, int]) -> None:
    inputs, holdings = split_snapshot(snapshot)
    holdings[CIRCULATION.return_sensor_present.address] = 1
    inputs[CIRCULATION.flow_switch.address] = 2
    inputs[CIRCULATION.pump.address] = 100
    inputs[CIRCULATION.return_temperature.address] = 70
    server = FakeLambdatronic(inputs, holdings)
    address = await server.start()
    try:
        async with client_for(address) as client:
            values = await client.read_values(EQUIPMENT)
        assert values.circulation == CirculationValues(flow_switch=True, pump=100.0, return_temperature=35.0)
    finally:
        await server.stop()


async def test_read_values_without_circulation_and_with_missing_counter(snapshot: dict[str, int]) -> None:
    inputs, holdings = split_snapshot(snapshot)
    inputs[BOILER.pellet_counter_t.address] = 0xFFFF
    server = FakeLambdatronic(inputs, holdings)
    address = await server.start()
    try:
        async with client_for(address) as client:
            values = await client.read_values(Equipment((1,), (), ()))
        assert values.circulation is None
        assert values.pellet_counter is None
        assert 30711 not in values.raw
    finally:
        await server.stop()


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
    assert (hc1.room_setpoint_heating, hc1.room_setpoint_setback) == (20.0, 16.0)
    assert (hc1.frost_protection, hc1.pump_off_below_flow_setpoint) == (10.0, 20.0)
    assert hc1.buffer_overheat_protection == 120.0
    assert parameters.heating_circuits[2].buffer_overheat_protection == 120.0
    assert parameters.pellet_filling_start_1 == time(10, 0)
    assert parameters.pellet_filling_start_2 == time(22, 0)
    assert parameters.auto_feeder_off is False
    assert parameters.circulation_pump_off_above == 35.0
    # 42001 = 60 -> 30 °C; 40001 - 42005 = 70 - 15 = 55 °C.
    assert parameters.buffers == {
        1: BufferParameters(circuit_release=30.0, ignition_below_buffer_temperature=55.0)
    }
    assert parameters.flat()["buffer1_circuit_release"] == 30.0


async def test_read_parameters_edge_cases(snapshot: dict[str, int]) -> None:
    inputs, holdings = split_snapshot(snapshot)
    holdings[BOILER.pellet_filling_start_1.address] = 2400  # documented maximum, not a time of day
    holdings[BOILER.pellet_filling_start_2.address] = 0xFFFF
    holdings[BOILER.auto_feeder_off.address] = 1
    holdings[buffer(1).ignition_difference.address] = 0xFFFF
    server = FakeLambdatronic(inputs, holdings)
    address = await server.start()
    try:
        async with client_for(address) as client:
            parameters = await client.read_parameters(Equipment((), (), (1,)))
        assert parameters.pellet_filling_start_1 is None
        assert parameters.pellet_filling_start_2 is None
        assert parameters.auto_feeder_off is True
        assert parameters.circulation_pump_off_above is None
        assert parameters.buffers[1] == BufferParameters(30.0, None)
    finally:
        await server.stop()


async def test_write_filling_start_as_minutes(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        assert await client.write(BOILER.pellet_filling_start_1, 630) == 630
        assert await client.write(heating_circuit(1).heating_limit, -5) == -5
    assert fake.holding_registers[BOILER.pellet_filling_start_1.address] == 630
    assert fake.holding_registers[heating_circuit(1).heating_limit.address] == 0x10000 - 10


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


async def test_write_mode_accepts_alias_read_back(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    mode = heating_circuit(1).operating_mode
    fake.ignore_write.add(mode.address)
    fake.holding_registers[mode.address] = 11  # boiler reports mode 0 as 11
    async with client_for(fake_address) as client:
        assert await client.write(mode, 0) == 0


async def test_write_mode_rejects_alias_of_other_mode(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    mode = heating_circuit(1).operating_mode
    fake.ignore_write.add(mode.address)
    fake.holding_registers[mode.address] = 12  # alias of 4, not 1
    async with client_for(fake_address) as client:
        with pytest.raises(WriteRejectedError, match="read back"):
            await client.write(mode, 1)


async def test_close_during_read_values(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    client = LambdatronicClient(fake_address[0], fake_address[1], timeout=5.0, request_pause=0.0)
    await client.connect()
    fake.silence_next = 10
    read = asyncio.create_task(client.read_values(EQUIPMENT))
    await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await client.close()
    with pytest.raises(ConnectionFailedError):
        await read
    assert loop.time() - start < 0.5
    await asyncio.sleep(0.1)
    assert not client._modbus.connected
    assert fake.connections == 1


@pytest.mark.parametrize("slave", [0, 248, -1])
def test_slave_must_be_valid(slave: int) -> None:
    with pytest.raises(ValueError, match="slave"):
        LambdatronicClient("127.0.0.1", slave=slave)


def test_modbus_options_are_passed_through() -> None:
    client = LambdatronicClient(
        "127.0.0.1", slave=247, timeout=1.5, connect_timeout=2.5, retries=0, request_pause=0.1
    )
    modbus = client._modbus
    assert (modbus._timeout, modbus._connect_timeout, modbus._retries, modbus._request_pause) == (
        1.5,
        2.5,
        0,
        0.1,
    )
    defaults = LambdatronicClient("127.0.0.1", slave=1)._modbus
    assert (defaults._timeout, defaults._connect_timeout, defaults._retries) == (5.0, 5.0, 2)
