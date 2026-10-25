"""Tests for ModbusTcpClient against the fake server."""

import asyncio
import socket

import pytest

from froeling_lambdatronic.modbus.client import MAX_READ_COUNT, ModbusTcpClient
from froeling_lambdatronic.modbus.exceptions import (
    DeviceBusyError,
    DeviceFailureError,
    IllegalAddressError,
    IllegalFunctionError,
    ModbusConnectionError,
    ModbusResponseError,
    ModbusTimeoutError,
)
from tests.fake_server import FakeLambdatronic


def client_for(address: tuple[str, int], **kwargs: float) -> ModbusTcpClient:
    options: dict[str, float] = {"timeout": 0.2, "request_pause": 0.0} | kwargs
    return ModbusTcpClient(address[0], address[1], **options)  # type: ignore[arg-type]


async def test_read_input_and_holding(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        assert client.connected
        assert await client.read_input_registers(2, 4000, 2) == [2, 19]
        assert await client.read_holding_registers(2, 0, 1) == [140]
    assert not client.connected


async def test_write_register_echo(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        await client.write_register(2, 1067, 14)
    assert fake.holding_registers[1067] == 14


async def test_write_rejects_out_of_16bit_values(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        with pytest.raises(ValueError, match="16-bit"):
            await client.write_register(2, 0, 70000)


async def test_read_count_is_limited(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address) as client:
        with pytest.raises(ValueError, match="count"):
            await client.read_input_registers(2, 0, MAX_READ_COUNT + 1)


async def test_exception_responses_are_raised_without_retry(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    async with client_for(fake_address) as client:
        with pytest.raises(IllegalAddressError):
            await client.read_input_registers(2, 5000, 1)
        fake.illegal_function_on_write = True
        with pytest.raises(IllegalFunctionError):
            await client.write_register(2, 0, 140)
        fake.illegal_function_on_write = False
        fake.reject_write.add(0)
        before = len(fake.requests)
        with pytest.raises(DeviceFailureError):
            await client.write_register(2, 0, 250)
        assert len(fake.requests) == before + 1


async def test_device_busy_is_retried(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    fake.busy_next = 1
    async with client_for(fake_address) as client:
        assert await client.read_input_registers(2, 4000, 1) == [2]
    fake.busy_next = 5
    async with client_for(fake_address, retries=1) as client:
        with pytest.raises(DeviceBusyError):
            await client.read_input_registers(2, 4000, 1)


async def test_timeout_reconnects_and_retries(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    fake.silence_next = 1
    async with client_for(fake_address) as client:
        assert await client.read_input_registers(2, 4000, 1) == [2]
    assert fake.connections == 2


async def test_timeout_after_all_retries(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    fake.silence_next = 10
    async with client_for(fake_address, retries=1) as client:
        with pytest.raises(ModbusTimeoutError):
            await client.read_input_registers(2, 4000, 1)


async def test_stale_and_garbage_data_are_tolerated(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    async with client_for(fake_address) as client:
        fake.stale_response_next = True
        assert await client.read_input_registers(2, 4000, 1) == [2]
        fake.garbage_before_next = b"\x01\x02\x03\x04\x05\x06\x07"
        assert await client.read_input_registers(2, 4001, 1) == [19]


async def test_wrong_unit_is_a_response_error(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address, retries=0) as client:
        fake.wrong_unit_next = True
        with pytest.raises(ModbusResponseError):
            await client.read_input_registers(2, 4000, 1)


async def test_connection_closed_by_peer_reconnects(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    async with client_for(fake_address) as client:
        fake.close_next = True
        assert await client.read_input_registers(2, 4000, 1) == [2]


async def test_connect_failure_raises_connection_error() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    client = ModbusTcpClient("127.0.0.1", port, connect_timeout=0.2, retries=0, request_pause=0.0)
    with pytest.raises(ModbusConnectionError):
        await client.connect()
    with pytest.raises(ModbusConnectionError):
        await client.read_input_registers(2, 0, 1)


async def test_requests_are_serialized_with_pause(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    async with client_for(fake_address, request_pause=0.05) as client:
        loop = asyncio.get_running_loop()
        start = loop.time()
        results = await asyncio.gather(*(client.read_input_registers(2, 4000, 1) for _ in range(3)))
        assert results == [[2], [2], [2]]
        assert loop.time() - start >= 0.1


async def test_unexpected_function_code_is_response_error(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address, retries=0) as client:
        assert client._protocol is not None
        original = client._protocol.request

        async def wrong_function(unit_id: int, pdu: bytes, timeout: float) -> tuple[int, bytes]:  # noqa: ASYNC109
            unit, _ = await original(unit_id, pdu, timeout)
            return unit, b"\x10\x00"

        client._protocol.request = wrong_function  # type: ignore[method-assign]
        with pytest.raises(ModbusResponseError):
            await client.read_input_registers(2, 4000, 1)


async def test_short_read_response_is_response_error(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address, retries=0) as client:
        assert client._protocol is not None
        original = client._protocol.request

        async def short(unit_id: int, pdu: bytes, timeout: float) -> tuple[int, bytes]:  # noqa: ASYNC109
            unit, _ = await original(unit_id, pdu, timeout)
            return unit, b"\x04\x02\x00"

        client._protocol.request = short  # type: ignore[method-assign]
        with pytest.raises(ModbusResponseError):
            await client.read_input_registers(2, 4000, 1)


async def test_write_echo_mismatch(fake_address: tuple[str, int]) -> None:
    async with client_for(fake_address, retries=0) as client:
        assert client._protocol is not None
        original = client._protocol.request

        async def bad_echo(unit_id: int, pdu: bytes, timeout: float) -> tuple[int, bytes]:  # noqa: ASYNC109
            unit, _ = await original(unit_id, pdu, timeout)
            return unit, b"\x06\x00\x00\x00\x01"

        client._protocol.request = bad_echo  # type: ignore[method-assign]
        with pytest.raises(ModbusResponseError, match="echo"):
            await client.write_register(2, 0, 140)


async def test_close_while_device_is_silent_returns_quickly(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    fake.silence_next = 10
    client = client_for(fake_address, timeout=5.0)
    await client.connect()
    read = asyncio.create_task(client.read_input_registers(2, 4000, 1))
    await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await client.close()
    with pytest.raises(ModbusConnectionError, match="client closed"):
        await read
    assert loop.time() - start < 0.5
    assert not client.connected
    await asyncio.sleep(0.1)
    assert not client.connected
    assert fake.connections == 1


async def test_close_interrupts_backoff(fake: FakeLambdatronic, fake_address: tuple[str, int]) -> None:
    fake.busy_next = 10
    client = client_for(fake_address, retries=10)
    await client.connect()
    read = asyncio.create_task(client.read_input_registers(2, 4000, 1))
    await asyncio.sleep(0.15)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await client.close()
    with pytest.raises(ModbusConnectionError, match="client closed"):
        await read
    assert loop.time() - start < 0.2
    assert not client.connected
    assert fake.connections == 1


async def test_closed_client_does_not_reconnect_implicitly(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    client = client_for(fake_address)
    assert await client.read_input_registers(2, 4000, 1) == [2]
    await client.close()
    with pytest.raises(ModbusConnectionError, match="client closed"):
        await client.read_input_registers(2, 4000, 1)
    assert not client.connected
    assert fake.connections == 1
    await client.connect()
    assert await client.read_input_registers(2, 4000, 1) == [2]
    assert fake.connections == 2
    await client.close()


async def test_close_during_connect_discards_the_connection(fake_address: tuple[str, int]) -> None:
    client = client_for(fake_address)
    connecting = asyncio.create_task(client.connect())
    await asyncio.sleep(0)
    await client.close()
    with pytest.raises(ModbusConnectionError, match="client closed"):
        await connecting
    assert not client.connected


async def test_transaction_ids_continue_across_reconnects(
    fake: FakeLambdatronic, fake_address: tuple[str, int]
) -> None:
    fake.silence_next = 1
    async with client_for(fake_address) as client:
        assert await client.read_input_registers(2, 4000, 1) == [2]
        assert await client.read_input_registers(2, 4000, 1) == [2]
    assert fake.connections == 2
    first, *rest = fake.transaction_ids
    assert fake.transaction_ids == [(first - 1 + i) % 0xFFFF + 1 for i in range(len(rest) + 1)]


def test_transaction_ids_wrap_around() -> None:
    client = ModbusTcpClient("127.0.0.1")
    client._next_tid = 0xFFFF
    assert [client._allocate_tid() for _ in range(3)] == [0xFFFF, 1, 2]
