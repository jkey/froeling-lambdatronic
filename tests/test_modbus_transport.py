"""Tests for the Modbus TCP framing protocol."""

import asyncio
import struct
from unittest.mock import MagicMock

import pytest

from froeling_lambdatronic.modbus.exceptions import (
    DeviceBusyError,
    IllegalAddressError,
    ModbusConnectionError,
    ModbusExceptionResponse,
    ModbusTimeoutError,
    exception_from_response,
)
from froeling_lambdatronic.modbus.transport import ModbusTcpProtocol


def frame(tid: int, unit: int, pdu: bytes, protocol: int = 0) -> bytes:
    return struct.pack(">HHHB", tid, protocol, len(pdu) + 1, unit) + pdu


def connected_protocol() -> tuple[ModbusTcpProtocol, MagicMock]:
    protocol = ModbusTcpProtocol()
    transport = MagicMock(spec=asyncio.Transport)
    transport.is_closing.return_value = False
    protocol.connection_made(transport)
    return protocol, transport


def sent_tid(transport: MagicMock, call: int = -1) -> int:
    data: bytes = transport.write.call_args_list[call].args[0]
    return int(struct.unpack_from(">H", data)[0])


async def test_request_writes_mbap_frame_and_returns_response() -> None:
    protocol, transport = connected_protocol()
    task = asyncio.create_task(protocol.request(2, b"\x04\x0f\xa0\x00\x01", 1.0))
    await asyncio.sleep(0)
    data: bytes = transport.write.call_args.args[0]
    assert data[2:] == b"\x00\x00\x00\x06\x02\x04\x0f\xa0\x00\x01"
    protocol.data_received(frame(sent_tid(transport), 2, b"\x04\x02\x00\x02"))
    assert await task == (2, b"\x04\x02\x00\x02")


async def test_responses_are_matched_by_transaction_id() -> None:
    protocol, transport = connected_protocol()
    first = asyncio.create_task(protocol.request(2, b"\x03\x00\x00\x00\x01", 1.0))
    second = asyncio.create_task(protocol.request(2, b"\x03\x00\x01\x00\x01", 1.0))
    await asyncio.sleep(0)
    tid1, tid2 = sent_tid(transport, 0), sent_tid(transport, 1)
    protocol.data_received(frame(tid2, 2, b"\x03\x02\x00\x22") + frame(tid1, 2, b"\x03\x02\x00\x11"))
    assert await first == (2, b"\x03\x02\x00\x11")
    assert await second == (2, b"\x03\x02\x00\x22")


async def test_fragmented_response_is_reassembled() -> None:
    protocol, transport = connected_protocol()
    task = asyncio.create_task(protocol.request(2, b"\x04\x00\x00\x00\x01", 1.0))
    await asyncio.sleep(0)
    data = frame(sent_tid(transport), 2, b"\x04\x02\x01\x02")
    protocol.data_received(data[:5])
    protocol.data_received(data[5:])
    assert await task == (2, b"\x04\x02\x01\x02")


async def test_late_response_is_discarded_not_delivered_to_next_request(
    caplog: pytest.LogCaptureFixture,
) -> None:
    protocol, transport = connected_protocol()
    with pytest.raises(ModbusTimeoutError):
        await protocol.request(2, b"\x04\x00\x00\x00\x01", 0.01)
    stale_tid = sent_tid(transport)
    task = asyncio.create_task(protocol.request(2, b"\x04\x00\x01\x00\x01", 1.0))
    await asyncio.sleep(0)
    protocol.data_received(frame(stale_tid, 2, b"\x04\x02\x00\x99"))
    assert not task.done()
    assert "unexpected response" in caplog.text
    protocol.data_received(frame(sent_tid(transport), 2, b"\x04\x02\x00\x01"))
    assert await task == (2, b"\x04\x02\x00\x01")


async def test_garbage_before_frame_is_skipped() -> None:
    protocol, transport = connected_protocol()
    task = asyncio.create_task(protocol.request(2, b"\x04\x00\x00\x00\x01", 1.0))
    await asyncio.sleep(0)
    protocol.data_received(
        b"\xde\xad\xbe\xef\x12\x34\x56\x78" + frame(sent_tid(transport), 2, b"\x04\x02\x00\x07")
    )
    assert await task == (2, b"\x04\x02\x00\x07")


async def test_invalid_length_triggers_resync() -> None:
    protocol, transport = connected_protocol()
    task = asyncio.create_task(protocol.request(2, b"\x04\x00\x00\x00\x01", 1.0))
    await asyncio.sleep(0)
    bogus = struct.pack(">HHHB", 7, 0, 999, 2)
    protocol.data_received(bogus + frame(sent_tid(transport), 2, b"\x04\x02\x00\x08"))
    assert await task == (2, b"\x04\x02\x00\x08")


async def test_request_without_connection_raises() -> None:
    protocol = ModbusTcpProtocol()
    assert not protocol.is_connected
    with pytest.raises(ModbusConnectionError):
        await protocol.request(2, b"\x04\x00\x00\x00\x01", 1.0)


async def test_connection_lost_fails_pending_requests_and_notifies() -> None:
    lost: list[Exception | None] = []
    protocol = ModbusTcpProtocol(on_connection_lost=lost.append)
    transport = MagicMock(spec=asyncio.Transport)
    transport.is_closing.return_value = False
    protocol.connection_made(transport)
    task = asyncio.create_task(protocol.request(2, b"\x04\x00\x00\x00\x01", 1.0))
    await asyncio.sleep(0)
    protocol.connection_lost(None)
    with pytest.raises(ModbusConnectionError):
        await task
    assert lost == [None]
    assert not protocol.is_connected


def test_close_closes_transport() -> None:
    protocol, transport = connected_protocol()
    protocol.close()
    transport.close.assert_called_once()
    ModbusTcpProtocol().close()  # no transport: no error


def test_transaction_ids_wrap_around() -> None:
    protocol = ModbusTcpProtocol()
    protocol._next_tid = 0xFFFF
    assert protocol._allocate_tid() == 0xFFFF
    assert protocol._allocate_tid() == 1


def test_exception_mapping() -> None:
    err = exception_from_response(0x03, 0x02)
    assert isinstance(err, IllegalAddressError)
    assert (err.function_code, err.exception_code) == (0x03, 0x02)
    assert isinstance(exception_from_response(0x04, 0x06), DeviceBusyError)
    unknown = exception_from_response(0x06, 0x0B)
    assert type(unknown) is ModbusExceptionResponse
    assert unknown.exception_code == 0x0B
