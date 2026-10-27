# froeling-lambdatronic

Async Python client for Fröling boilers with **Lambdatronic 3200** control.

- **Local Modbus TCP** via an RS232-to-Ethernet gateway (e.g. Waveshare RS232/485-TO-ETH in
  "Modbus TCP gateway" mode) on the boiler's COM2: read all values, read and write customer
  parameters (heating curve, setback, heating limits, room setpoints, frost protection, hot water,
  buffer, pellet filling times, circulation pump, boiler setpoint, heating circuit operating mode).
- **Fröling Connect** (cloud): read all components in one snapshot, write parameters (including
  values Modbus cannot write, e.g. the system operating mode), read and write weekly schedules,
  read system information, connection state and the error history.

Both sources use the same value keys (`boiler_temperature`, `hc1_flow_temperature`,
`dhw1_setpoint`, …), so an application can combine them.

No Modbus library dependency; only `aiohttp`. Fully typed (`py.typed`), asyncio only.

## Boiler prerequisites

Service menu (code −7) → MODBUS settings: *COM 2 als MODBUS Schnittstelle = JA*, *MODBUS Adresse*
(default 2), *MODBUS Protokoll = 1 (RTU)*, **MODBUS-Protokoll 2014 verwenden = JA** (required for
writing). Serial: 57600 baud, 8N1, no handshake. Use a straight cable to COM2 on the control board.

## Usage

```python
import asyncio
from froeling_lambdatronic import LambdatronicClient, heating_circuit


async def main() -> None:
    async with LambdatronicClient("192.168.1.50", slave=2) as boiler:
        equipment = await boiler.detect_equipment()
        values = await boiler.read_values(equipment)
        print(values.boiler_temperature, values.system_state)
        parameters = await boiler.read_parameters(equipment)
        print(parameters.heating_circuits[1].flow_for_outside(0))
        await boiler.write(heating_circuit(1).curve_flow_plus10, 46)


asyncio.run(main())
```

```python
import asyncio

import aiohttp

from froeling_lambdatronic import FroelingConnectClient, OperatingMode


async def main() -> None:
    async with aiohttp.ClientSession() as session:
        cloud = FroelingConnectClient(session, "user@example.com", "secret")
        facility = (await cloud.get_facilities())[0]
        await cloud.set_operating_mode(facility.facility_id, OperatingMode.DOMESTIC_HOT_WATER)


asyncio.run(main())
```

Writing is validated before sending (writable register, range, step) and verified by reading the
register back. The gateway must not be polled by other clients at the same time.

### Modbus values and settings

`read_values()` returns `LambdatronicValues`, `read_parameters()` returns `LambdatronicParameters`.
Both offer `flat()`, a dict of all values under full keys, and `register_for_key()` finds the
register to write for such a key:

```python
from datetime import time

from froeling_lambdatronic import BOILER, register_for_key, time_to_minutes

values = (await boiler.read_values(equipment)).flat()
print(values["pellet_counter"], values["hc1_flow_temperature"])  # kg, °C

parameters = await boiler.read_parameters(equipment)
print(parameters.pellet_filling_start_1)  # datetime.time(10, 0)
await boiler.write(BOILER.pellet_filling_start_1, time_to_minutes(time(9, 30)))

definition = register_for_key("hc2_frost_protection")  # None for derived or unknown keys
if definition is not None:
    await boiler.write(definition, 5)
```

New in 0.2.0: hours since maintenance, the full resettable pellet counter in kg, room setpoints,
frost protection, pump-off threshold and buffer overheat protection per heating circuit, buffer
circuit release and the derived "ignition below buffer temperature", pellet filling start times,
"disable automatic feeding" and the circulation pump (detected as `Equipment.circulation_pump`).

## Fröling Connect

Fröling Connect has no public API. This library uses the endpoints of the Fröling web portal; they
may change without notice. Keep polling slow (the boiler reports about once a minute); HTTP 429 is
raised as `RateLimitedError` with `retry_after`. Writing requires *Fernschalten über connect
möglich* on the boiler, otherwise `RemoteControlDisabledError` is raised.

```python
from datetime import time

from froeling_lambdatronic import Phase, Weekday

cloud = FroelingConnectClient(session, "user@example.com", "secret")
facility = (await cloud.get_facilities())[0].facility_id

snapshot = await cloud.read_snapshot(facility, details=True)  # overview + every component detail
print(snapshot.value("buffer1_charge"), snapshot.value("hc1_pump"))  # cloud-only values

await cloud.set_value(facility, "pellet_filling_start_2", time(22, 0))  # read-only via Modbus
await cloud.set_value(facility, "hc1_operating_mode", 3)

schedule = await cloud.get_schedule(facility, "hc1")
await cloud.set_schedule(facility, "hc1", {Weekday.SATURDAY: [Phase(7 * 60, 22 * 60)]})

info = await cloud.get_system_info(facility)  # versions, equipment number, operating data
header = await cloud.get_header(facility)  # header.online, header.last_signal
errors = await cloud.get_errors(facility)  # error history with timestamp and severity
```

`details` is a required keyword argument of `read_snapshot()`: `details=False` needs a single request;
`details=True` adds the component list and one request per component (`skip={"hc2"}` leaves
components out). A component detail that cannot be parsed or is not found (HTTP 404) is skipped and
logged; connection, timeout, rate limiting, authentication and login errors (including an unusable token on renewal) abort the
snapshot. `set_schedule()`
replaces only the given days; the request still carries all seven days, the other days keep their
current phases. A day has at most four phases in minutes after midnight (end 1440 = 24:00). If a
day that is not replaced contains a phase the library cannot read, nothing is written
(`CommunicationError`) so that phase is not lost.
`CLOUD_REFS` documents which key maps to which Fröling Connect parameter; `merge_equipment()`
compares the equipment detected via Modbus with the cloud components. System information never
contains the address, contacts or pairing code of the facility.

## Development

```bash
uv sync
uv run ruff check && uv run ruff format --check . && uv run mypy && uv run pytest
```

Register documentation: Fröling "Kommunikationsprotokoll ModBus Lambdatronic 3200", B1200522.

## Changelog

### 0.2.0

Breaking changes:

- `boiler_output` (30015) is now `boiler_control_output`: it is the requested modulation of the
  output controller ("Kesselstellgröße"), not the delivered output.
- `LambdatronicValues.pellet_counter_kg` is replaced by `pellet_counter`, the whole resettable
  counter in kg (1000 · 30083 + 30082). The register 30082 alone is only the kg part.
- Heating limits (`hcN_heating_limit`, `hcN_heating_limit_setback`) accept −20…50 °C (was 0…50).
- `HeatingCircuitParameters`, `LambdatronicValues` and `LambdatronicParameters` have new required
  fields; `BufferRegisters.all()` also contains the buffer settings.
- The constructors of `HeatingCircuitRegisters` (`room_setpoint_heating`, `frost_protection`,
  `pump_off_below_flow_setpoint`, `room_setpoint_setback`, `buffer_overheat_protection`) and
  `BufferRegisters` (`circuit_release`, `ignition_difference`) have new required fields; use
  `heating_circuit(n)` and `buffer(n)` to build them.

New: the Modbus registers listed above, `flat()`, `register_for_key()`, `minutes_to_time()` /
`time_to_minutes()`, and the Fröling Connect snapshot, writing, schedules, system information,
header, error history and `RateLimitedError`; `Facility.equipment_number`.
