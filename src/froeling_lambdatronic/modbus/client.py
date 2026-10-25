"""Minimal asyncio Modbus TCP client for FC03, FC04 and FC06."""

import asyncio
import logging
import struct
import time
from types import TracebackType
from typing import Self

from .exceptions import (
    DeviceBusyError,
    ModbusConnectionError,
    ModbusError,
    ModbusResponseError,
    ModbusTimeoutError,
    exception_from_response,
)
from .transport import ModbusTcpProtocol

_LOGGER = logging.getLogger(__name__)

READ_HOLDING_REGISTERS = 0x03
READ_INPUT_REGISTERS = 0x04
WRITE_SINGLE_REGISTER = 0x06
# Upper limit of registers per read on the Lambdatronic 3200.
MAX_READ_COUNT = 122

_TRANSPORT_ERRORS = (ModbusConnectionError, ModbusTimeoutError, ModbusResponseError, OSError)


class ModbusTcpClient:
    """One persistent connection, one request at a time, reconnect on transport errors."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        *,
        timeout: float = 5.0,
        connect_timeout: float = 5.0,
        request_pause: float = 0.03,
        retries: int = 2,
        backoff_max: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout
        self._connect_timeout = connect_timeout
        self._request_pause = request_pause
        self._retries = retries
        self._backoff_max = backoff_max
        self._lock = asyncio.Lock()
        self._protocol: ModbusTcpProtocol | None = None
        self._last_request_end = 0.0

    @property
    def connected(self) -> bool:
        """Return True while a connection is open."""
        return self._protocol is not None and self._protocol.is_connected

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the connection if it is not open yet."""
        async with self._lock:
            await self._connect()

    async def close(self) -> None:
        """Close the connection."""
        async with self._lock:
            self._disconnect()

    async def read_holding_registers(self, unit_id: int, address: int, count: int) -> list[int]:
        """Read holding registers (FC03) starting at a zero-based address."""
        return await self._read(READ_HOLDING_REGISTERS, unit_id, address, count)

    async def read_input_registers(self, unit_id: int, address: int, count: int) -> list[int]:
        """Read input registers (FC04) starting at a zero-based address."""
        return await self._read(READ_INPUT_REGISTERS, unit_id, address, count)

    async def write_register(self, unit_id: int, address: int, value: int) -> None:
        """Write one holding register (FC06) and verify the echo."""
        if not 0 <= value <= 0xFFFF:
            raise ValueError(f"value {value} does not fit into a 16-bit register")
        pdu = struct.pack(">BHH", WRITE_SINGLE_REGISTER, address, value)
        response = await self._execute(unit_id, pdu)
        if response != pdu:
            raise ModbusResponseError(f"write echo mismatch: sent {pdu.hex()}, got {response.hex()}")

    async def _read(self, function: int, unit_id: int, address: int, count: int) -> list[int]:
        if not 1 <= count <= MAX_READ_COUNT:
            raise ValueError(f"count must be between 1 and {MAX_READ_COUNT}, got {count}")
        response = await self._execute(unit_id, struct.pack(">BHH", function, address, count))
        if len(response) != 2 + 2 * count or response[1] != 2 * count:
            raise ModbusResponseError(f"unexpected response length {len(response)} for {count} registers")
        return list(struct.unpack(f">{count}H", response[2:]))

    async def _execute(self, unit_id: int, pdu: bytes) -> bytes:
        async with self._lock:
            attempt = 0
            while True:
                try:
                    return await self._execute_once(unit_id, pdu)
                except DeviceBusyError:
                    if attempt >= self._retries:
                        raise
                    _LOGGER.debug("Device busy, retrying")
                except _TRANSPORT_ERRORS as err:
                    self._disconnect()
                    if attempt >= self._retries:
                        if isinstance(err, ModbusError):
                            raise
                        raise ModbusConnectionError(str(err)) from err
                    _LOGGER.debug("Request failed (%s), reconnecting", err)
                await asyncio.sleep(min(self._backoff_max, 0.1 * 2**attempt))
                attempt += 1

    async def _execute_once(self, unit_id: int, pdu: bytes) -> bytes:
        await self._connect()
        wait = self._request_pause - (time.monotonic() - self._last_request_end)
        if wait > 0:
            await asyncio.sleep(wait)
        assert self._protocol is not None
        try:
            unit, response = await self._protocol.request(unit_id, pdu, self._timeout)
        finally:
            self._last_request_end = time.monotonic()
        if unit != unit_id:
            raise ModbusResponseError(f"unit id mismatch: expected {unit_id}, got {unit}")
        if not response:
            raise ModbusResponseError("empty response")
        if response[0] == pdu[0] | 0x80:
            raise exception_from_response(pdu[0], response[1] if len(response) > 1 else 0)
        if response[0] != pdu[0]:
            raise ModbusResponseError(f"unexpected function code {response[0]:#04x}")
        return response

    async def _connect(self) -> None:
        if self.connected:
            return
        loop = asyncio.get_running_loop()
        try:
            _, protocol = await asyncio.wait_for(
                loop.create_connection(ModbusTcpProtocol, self._host, self._port),
                self._connect_timeout,
            )
        except (OSError, TimeoutError) as err:
            raise ModbusConnectionError(f"cannot connect to {self._host}:{self._port}: {err}") from err
        self._protocol = protocol
        _LOGGER.debug("Connected to %s:%d", self._host, self._port)

    def _disconnect(self) -> None:
        if self._protocol is not None:
            self._protocol.close()
            self._protocol = None
