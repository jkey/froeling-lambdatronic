"""asyncio protocol implementing Modbus TCP (MBAP) framing."""

import asyncio
import logging
import struct
from collections.abc import Callable

from .exceptions import ModbusConnectionError, ModbusTimeoutError

_LOGGER = logging.getLogger(__name__)

MBAP_HEADER = struct.Struct(">HHHB")
# Largest MBAP length value: unit id (1 byte) plus a 253 byte PDU.
MAX_LENGTH = 254


class ModbusTcpProtocol(asyncio.Protocol):
    """Frames requests, matches responses by transaction id and resyncs on garbage."""

    def __init__(self, on_connection_lost: Callable[[Exception | None], None] | None = None) -> None:
        self._transport: asyncio.Transport | None = None
        self._buffer = bytearray()
        self._next_tid = 1
        self._pending: dict[int, asyncio.Future[tuple[int, bytes]]] = {}
        self._on_connection_lost = on_connection_lost

    @property
    def is_connected(self) -> bool:
        """Return True while the transport is open."""
        return self._transport is not None and not self._transport.is_closing()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        """Store the transport once the connection is up."""
        assert isinstance(transport, asyncio.Transport)
        self._transport = transport

    def connection_lost(self, exc: Exception | None) -> None:
        """Fail all pending requests and notify the owner."""
        self._transport = None
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ModbusConnectionError("connection lost before a response arrived"))
        self._pending.clear()
        if self._on_connection_lost is not None:
            self._on_connection_lost(exc)

    def close(self) -> None:
        """Close the underlying transport."""
        if self._transport is not None:
            self._transport.close()

    def _allocate_tid(self) -> int:
        tid = self._next_tid
        self._next_tid = tid % 0xFFFF + 1
        return tid

    async def request(self, unit_id: int, pdu: bytes, timeout: float) -> tuple[int, bytes]:  # noqa: ASYNC109
        """Send a PDU and return (unit id, response PDU) of the matching response."""
        if self._transport is None or self._transport.is_closing():
            raise ModbusConnectionError("not connected")
        tid = self._allocate_tid()
        future: asyncio.Future[tuple[int, bytes]] = asyncio.get_running_loop().create_future()
        self._pending[tid] = future
        self._transport.write(MBAP_HEADER.pack(tid, 0, len(pdu) + 1, unit_id) + pdu)
        try:
            return await asyncio.wait_for(future, timeout)
        except TimeoutError as err:
            raise ModbusTimeoutError(f"no response for transaction {tid} within {timeout}s") from err
        finally:
            self._pending.pop(tid, None)

    def data_received(self, data: bytes) -> None:
        """Parse complete frames from the receive buffer."""
        self._buffer.extend(data)
        while len(self._buffer) >= MBAP_HEADER.size:
            tid, protocol_id, length, unit_id = MBAP_HEADER.unpack_from(self._buffer)
            if protocol_id != 0 or not 2 <= length <= MAX_LENGTH:
                self._resync()
                continue
            total = 6 + length
            if len(self._buffer) < total:
                return
            pdu = bytes(self._buffer[MBAP_HEADER.size : total])
            del self._buffer[:total]
            future = self._pending.get(tid)
            if future is None or future.done():
                _LOGGER.warning("Discarding unexpected response for transaction %d: %s", tid, pdu.hex(" "))
                continue
            future.set_result((unit_id, pdu))

    def _resync(self) -> None:
        """Drop bytes until the buffer may start with a valid MBAP header."""
        buffer = self._buffer
        start = len(buffer)
        for candidate in range(1, len(buffer)):
            if len(buffer) - candidate < 6:
                start = candidate
                break
            protocol_id, length = struct.unpack_from(">HH", buffer, candidate + 2)
            if protocol_id == 0 and 2 <= length <= MAX_LENGTH:
                start = candidate
                break
        _LOGGER.debug("Discarding %d garbage bytes", start)
        del buffer[:start]
