"""High-level client for a Lambdatronic 3200 boiler."""

from collections.abc import Iterable
from datetime import time
from types import TracebackType
from typing import Self

from .exceptions import (
    CommunicationError,
    ConnectionFailedError,
    NoResponseError,
    ProtocolNotEnabledError,
    WriteRejectedError,
)
from .modbus.client import ModbusTcpClient
from .modbus.exceptions import (
    DeviceFailureError,
    IllegalFunctionError,
    IllegalValueError,
    ModbusConnectionError,
    ModbusError,
    ModbusTimeoutError,
)
from .models import (
    MINUTES_PER_DAY,
    BufferParameters,
    BufferValues,
    CirculationValues,
    Equipment,
    HeatingCircuitParameters,
    HeatingCircuitValues,
    HotWaterParameters,
    HotWaterValues,
    LambdatronicParameters,
    LambdatronicValues,
    minutes_to_time,
)
from .registers import (
    BOILER,
    CIRCULATION,
    ERROR_SLOTS,
    MAX_BUFFERS,
    MAX_HEATING_CIRCUITS,
    MAX_HOT_WATER,
    BufferRegisters,
    ReadBlock,
    RegisterDef,
    Space,
    buffer,
    heating_circuit,
    hot_water,
    plan_blocks,
)
from .texts import normalize_heating_circuit_mode

# Highest unicast Modbus unit id.
MAX_SLAVE = 247

_VALUE_DEFS = tuple(d for d in BOILER.all() if d.space is Space.INPUT)
_PARAMETER_DEFS = tuple(d for d in BOILER.all() if d.space is Space.HOLDING)


class LambdatronicClient:
    """Reads and writes a Lambdatronic 3200 via a Modbus TCP gateway."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave: int = 2,
        *,
        timeout: float = 5.0,
        connect_timeout: float = 5.0,
        retries: int = 2,
        request_pause: float = 0.03,
        modbus: ModbusTcpClient | None = None,
    ) -> None:
        if not 1 <= slave <= MAX_SLAVE:
            raise ValueError(f"slave must be between 1 and {MAX_SLAVE}, got {slave}")
        self._slave = slave
        self._modbus = modbus or ModbusTcpClient(
            host,
            port,
            timeout=timeout,
            connect_timeout=connect_timeout,
            retries=retries,
            request_pause=request_pause,
        )

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
        """Open the connection to the gateway."""
        try:
            await self._modbus.connect()
        except ModbusConnectionError as err:
            raise ConnectionFailedError(str(err)) from err

    async def close(self) -> None:
        """Close the connection."""
        await self._modbus.close()

    async def probe(self) -> int:
        """Read the system state (34001) to check that the boiler answers."""
        raw = await self._read_raw([BOILER.system_state])
        return raw[BOILER.system_state.number]

    async def detect_equipment(self) -> Equipment:
        """Find installed heating circuits, hot water tanks and buffers."""
        circuits = [heating_circuit(n) for n in range(1, MAX_HEATING_CIRCUITS + 1)]
        tanks = [hot_water(n) for n in range(1, MAX_HOT_WATER + 1)]
        buffers = [buffer(n) for n in range(1, MAX_BUFFERS + 1)]
        definitions: list[RegisterDef] = []
        for hc in circuits:
            definitions += [hc.flow_temperature, hc.room_temperature]
        definitions += [t.temperature for t in tanks]
        for b in buffers:
            definitions += [b.top, b.bottom]
        definitions.append(CIRCULATION.pump_off_above)
        raw = await self._read_raw(definitions, max_gap=2)

        def present(*defs: RegisterDef) -> bool:
            return any(raw[d.number] not in (0, 0xFFFF) for d in defs)

        return Equipment(
            heating_circuits=tuple(
                hc.number for hc in circuits if present(hc.flow_temperature, hc.room_temperature)
            ),
            hot_water=tuple(t.number for t in tanks if present(t.temperature)),
            buffers=tuple(b.number for b in buffers if present(b.top, b.bottom)),
            # An undocumented register answers 0xFFFF; 40601 exists only with a circulation pump.
            circulation_pump=raw[CIRCULATION.pump_off_above.number] != 0xFFFF,
        )

    async def read_values(self, equipment: Equipment) -> LambdatronicValues:
        """Read all current values for the given equipment."""
        circuits = {n: heating_circuit(n) for n in equipment.heating_circuits}
        tanks = {n: hot_water(n) for n in equipment.hot_water}
        buffers = {n: buffer(n) for n in equipment.buffers}
        definitions = [*_VALUE_DEFS, *ERROR_SLOTS]
        for hc in circuits.values():
            definitions += [hc.flow_temperature, hc.flow_setpoint, hc.room_temperature]
        for t in tanks.values():
            definitions += [t.temperature, t.pump]
        for b in buffers.values():
            definitions += [b.top, b.middle, b.bottom, b.pump]
        if equipment.circulation_pump:
            definitions += CIRCULATION.all()
        raw = await self._read_raw(definitions)

        def value(definition: RegisterDef) -> float | None:
            return definition.decode(raw[definition.number])

        def code(definition: RegisterDef) -> int | None:
            decoded = value(definition)
            return None if decoded is None else int(decoded)

        demand = code(BOILER.boiler_demand)
        tonnes = value(BOILER.pellet_counter_t)
        kilograms = value(BOILER.pellet_counter_kg)
        return LambdatronicValues(
            boiler_temperature=value(BOILER.boiler_temperature),
            flue_gas_temperature=value(BOILER.flue_gas_temperature),
            residual_oxygen=value(BOILER.residual_oxygen),
            induced_draft_speed=value(BOILER.induced_draft_speed),
            return_temperature=value(BOILER.return_temperature),
            boiler_control_output=value(BOILER.boiler_control_output),
            operating_hours=value(BOILER.operating_hours),
            hours_since_maintenance=value(BOILER.hours_since_maintenance),
            pellet_hopper_level=value(BOILER.pellet_hopper_level),
            burner_starts=value(BOILER.burner_starts),
            feed_rate=value(BOILER.feed_rate),
            boiler_setpoint_calculated=value(BOILER.boiler_setpoint_calculated),
            heating_hours=value(BOILER.heating_hours),
            pellet_counter=None if tonnes is None or kilograms is None else 1000 * tonnes + kilograms,
            pellet_consumption_total=value(BOILER.pellet_consumption_total),
            hours_until_ash_emptying=value(BOILER.hours_until_ash_emptying),
            outside_temperature=value(BOILER.outside_temperature),
            boiler_demand=None if demand is None else demand == 1,
            system_state=code(BOILER.system_state),
            boiler_state=code(BOILER.boiler_state),
            errors=tuple(raw[d.number] for d in ERROR_SLOTS if raw[d.number] != 0xFFFF),
            heating_circuits={
                n: HeatingCircuitValues(
                    value(hc.flow_temperature), value(hc.flow_setpoint), value(hc.room_temperature)
                )
                for n, hc in circuits.items()
            },
            hot_water={n: HotWaterValues(value(t.temperature), value(t.pump)) for n, t in tanks.items()},
            buffers={
                n: BufferValues(value(b.top), value(b.middle), value(b.bottom), value(b.pump))
                for n, b in buffers.items()
            },
            circulation=self._circulation(raw) if equipment.circulation_pump else None,
            raw=raw,
        )

    @staticmethod
    def _circulation(raw: dict[int, int]) -> CirculationValues:
        flow_switch = CIRCULATION.flow_switch.decode(raw[CIRCULATION.flow_switch.number])
        sensor = raw[CIRCULATION.return_sensor_present.number]
        return CirculationValues(
            flow_switch=None if flow_switch is None else flow_switch != 0,
            pump=CIRCULATION.pump.decode(raw[CIRCULATION.pump.number]),
            return_temperature=(
                CIRCULATION.return_temperature.decode(raw[CIRCULATION.return_temperature.number])
                if sensor == 1
                else None
            ),
        )

    async def read_parameters(self, equipment: Equipment) -> LambdatronicParameters:
        """Read all writable settings for the given equipment."""
        circuits = {n: heating_circuit(n) for n in equipment.heating_circuits}
        tanks = {n: hot_water(n) for n in equipment.hot_water}
        buffers = {n: buffer(n) for n in equipment.buffers}
        definitions = list(_PARAMETER_DEFS)
        for hc in circuits.values():
            definitions += [
                hc.curve_flow_plus10,
                hc.curve_flow_minus10,
                hc.setback,
                hc.max_flow,
                hc.heating_limit,
                hc.heating_limit_setback,
                hc.operating_mode,
                hc.room_setpoint_heating,
                hc.room_setpoint_setback,
                hc.frost_protection,
                hc.pump_off_below_flow_setpoint,
                hc.buffer_overheat_protection,
            ]
        for t in tanks.values():
            definitions += [t.setpoint, t.recharge_below]
        for b in buffers.values():
            definitions += [b.circuit_release, b.ignition_difference]
        if equipment.circulation_pump:
            definitions.append(CIRCULATION.pump_off_above)
        raw = await self._read_raw(definitions)

        def value(definition: RegisterDef) -> float | None:
            return definition.decode(raw[definition.number])

        def time_of_day(definition: RegisterDef) -> time | None:
            minutes = value(definition)
            if minutes is None or not 0 <= minutes < MINUTES_PER_DAY:
                return None
            return minutes_to_time(int(minutes))

        def ignition_below(b: BufferRegisters) -> float | None:
            setpoint = value(BOILER.boiler_setpoint)
            difference = value(b.ignition_difference)
            return None if setpoint is None or difference is None else setpoint - difference

        feeder_off = value(BOILER.auto_feeder_off)
        return LambdatronicParameters(
            boiler_setpoint=value(BOILER.boiler_setpoint),
            pellet_stock=value(BOILER.pellet_stock),
            pellet_stock_minimum=value(BOILER.pellet_stock_minimum),
            pellet_filling_start_1=time_of_day(BOILER.pellet_filling_start_1),
            pellet_filling_start_2=time_of_day(BOILER.pellet_filling_start_2),
            auto_feeder_off=None if feeder_off is None else feeder_off != 0,
            circulation_pump_off_above=(
                value(CIRCULATION.pump_off_above) if equipment.circulation_pump else None
            ),
            heating_circuits={
                n: HeatingCircuitParameters(
                    curve_flow_plus10=value(hc.curve_flow_plus10),
                    curve_flow_minus10=value(hc.curve_flow_minus10),
                    setback=value(hc.setback),
                    max_flow=value(hc.max_flow),
                    heating_limit=value(hc.heating_limit),
                    heating_limit_setback=value(hc.heating_limit_setback),
                    operating_mode=normalize_heating_circuit_mode(raw[hc.operating_mode.number]),
                    room_setpoint_heating=value(hc.room_setpoint_heating),
                    room_setpoint_setback=value(hc.room_setpoint_setback),
                    frost_protection=value(hc.frost_protection),
                    pump_off_below_flow_setpoint=value(hc.pump_off_below_flow_setpoint),
                    buffer_overheat_protection=value(hc.buffer_overheat_protection),
                )
                for n, hc in circuits.items()
            },
            hot_water={
                n: HotWaterParameters(value(t.setpoint), value(t.recharge_below)) for n, t in tanks.items()
            },
            buffers={
                n: BufferParameters(value(b.circuit_release), ignition_below(b)) for n, b in buffers.items()
            },
            raw=raw,
        )

    async def write(self, definition: RegisterDef, value: float) -> float:
        """Validate, write one setting and return the value read back from the boiler."""
        raw_value = definition.encode(value)
        try:
            await self._modbus.write_register(self._slave, definition.address, raw_value)
        except IllegalFunctionError as err:
            raise ProtocolNotEnabledError(
                definition.key, "writing is disabled (MODBUS-Protokoll 2014)"
            ) from err
        except (IllegalValueError, DeviceFailureError) as err:
            raise WriteRejectedError(definition.key, "value rejected by the boiler") from err
        except ModbusError as err:
            raise self._map(err) from err
        read_back = (await self._read_raw([definition]))[definition.number]
        if definition.key.endswith("_operating_mode"):
            # The boiler reads heating circuit modes back with aliases (e.g. 11 for 0).
            normalized = normalize_heating_circuit_mode(read_back)
            if normalized != raw_value:
                raise WriteRejectedError(definition.key, f"read back {read_back} instead of {raw_value}")
            return float(normalized)
        if read_back != raw_value:
            raise WriteRejectedError(definition.key, f"read back {read_back} instead of {raw_value}")
        decoded = definition.decode(read_back)
        if decoded is None:
            raise CommunicationError(f"write to {definition.key} could not be verified")
        return decoded

    async def _read_raw(self, definitions: Iterable[RegisterDef], max_gap: int = 10) -> dict[int, int]:
        return await self._read_blocks(plan_blocks(definitions, max_gap=max_gap))

    async def _read_blocks(self, blocks: list[ReadBlock]) -> dict[int, int]:
        raw: dict[int, int] = {}
        for block in blocks:
            try:
                if block.space is Space.INPUT:
                    values = await self._modbus.read_input_registers(self._slave, block.address, block.count)
                    base = 30001
                else:
                    values = await self._modbus.read_holding_registers(
                        self._slave, block.address, block.count
                    )
                    base = 40001
            except ModbusError as err:
                raise self._map(err) from err
            for offset, item in enumerate(values):
                raw[base + block.address + offset] = item
        return raw

    @staticmethod
    def _map(err: ModbusError) -> Exception:
        if isinstance(err, ModbusTimeoutError):
            return NoResponseError(str(err))
        if isinstance(err, ModbusConnectionError):
            return ConnectionFailedError(str(err))
        return CommunicationError(str(err))
