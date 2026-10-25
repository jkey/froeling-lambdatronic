"""Fake Lambdatronic Modbus TCP server with fault injection."""

import asyncio
import contextlib
import struct

INPUT_RANGES = (range(0, 2625), range(3000, 3020), range(4000, 4002))
HOLDING_RANGES = (range(0, 3040), range(8000, 8064))


def _valid(ranges: tuple[range, ...], address: int, count: int) -> bool:
    return any(address in r and address + count - 1 in r for r in ranges)


def split_snapshot(snapshot: dict[str, int]) -> tuple[dict[int, int], dict[int, int]]:
    """Turn {"30001": raw, "40001": raw} into (input, holding) dicts keyed by zero-based address."""
    inputs: dict[int, int] = {}
    holdings: dict[int, int] = {}
    for number, raw in snapshot.items():
        n = int(number)
        if 30001 <= n < 40001:
            inputs[n - 30001] = raw
        else:
            holdings[n - 40001] = raw
    return inputs, holdings


class FakeLambdatronic:
    """Serves registers like the Waveshare gateway in front of a Lambdatronic 3200."""

    def __init__(
        self, input_registers: dict[int, int], holding_registers: dict[int, int], unit_id: int = 2
    ) -> None:
        self.input_registers = input_registers
        self.holding_registers = holding_registers
        self.unit_id = unit_id
        self.requests: list[tuple[int, int, int, int]] = []
        self.silence_next = 0
        self.busy_next = 0
        self.garbage_before_next = b""
        self.stale_response_next = False
        self.close_next = False
        self.wrong_unit_next = False
        self.illegal_function_on_write = False
        self.reject_write: set[int] = set()
        self.ignore_write: set[int] = set()
        self.connections = 0
        self._server: asyncio.Server | None = None
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        host, port = self._server.sockets[0].getsockname()[:2]
        return str(host), int(port)

    async def stop(self) -> None:
        for writer in self._writers:
            writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        self._writers.append(writer)
        try:
            while True:
                header = await reader.readexactly(7)
                tid, _, length, unit = struct.unpack(">HHHB", header)
                pdu = await reader.readexactly(length - 1)
                if self.close_next:
                    self.close_next = False
                    writer.close()
                    return
                response = self._process(unit, pdu)
                if response is None:
                    continue
                answer_unit = unit + 1 if self.wrong_unit_next else unit
                self.wrong_unit_next = False
                out = self.garbage_before_next
                self.garbage_before_next = b""
                if self.stale_response_next:
                    self.stale_response_next = False
                    out += (
                        struct.pack(">HHHB", (tid + 100) % 0xFFFF, 0, len(response) + 1, answer_unit)
                        + response
                    )
                out += struct.pack(">HHHB", tid, 0, len(response) + 1, answer_unit) + response
                writer.write(out)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    def _process(self, unit: int, pdu: bytes) -> bytes | None:  # noqa: PLR0911
        function, address, value = struct.unpack(">BHH", pdu[:5])
        self.requests.append((unit, function, address, value))
        if unit != self.unit_id or self.silence_next > 0:
            self.silence_next = max(0, self.silence_next - 1)
            return None
        if self.busy_next > 0:
            self.busy_next -= 1
            return bytes([function | 0x80, 0x06])
        if function in (3, 4):
            ranges = INPUT_RANGES if function == 4 else HOLDING_RANGES
            registers = self.input_registers if function == 4 else self.holding_registers
            if value > 122:
                return bytes([function | 0x80, 0x03])
            if not _valid(ranges, address, value):
                return bytes([function | 0x80, 0x02])
            data = [registers.get(address + i, 0xFFFF) for i in range(value)]
            return bytes([function, 2 * value]) + struct.pack(f">{value}H", *data)
        if function == 6:
            if self.illegal_function_on_write:
                return bytes([0x86, 0x01])
            if address in self.reject_write:
                return bytes([0x86, 0x04])
            if not _valid(HOLDING_RANGES, address, 1):
                return bytes([0x86, 0x02])
            if address not in self.ignore_write:
                self.holding_registers[address] = value
            return pdu[:5]
        return bytes([function | 0x80, 0x01])
