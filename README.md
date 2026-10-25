# froeling-lambdatronic

Async Python client for Fröling boilers with **Lambdatronic 3200** control.

- **Local Modbus TCP** via an RS232-to-Ethernet gateway (e.g. Waveshare RS232/485-TO-ETH in
  "Modbus TCP gateway" mode) on the boiler's COM2: read all values, read and write customer
  parameters (heating curve, setback, heating limits, hot water, boiler setpoint, heating circuit
  operating mode).
- **Fröling Connect** (cloud): switch the system operating mode (Automatik / Brauchwasser /
  Dauerlast), which is read-only via Modbus.

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
    async with LambdatronicClient("192.168.70.18", slave=2) as boiler:
        equipment = await boiler.detect_equipment()
        values = await boiler.read_values(equipment)
        print(values.boiler_temperature, values.system_state)
        parameters = await boiler.read_parameters(equipment)
        print(parameters.heating_circuits[1].flow_for_outside(0))
        await boiler.write(heating_circuit(1).curve_flow_plus10, 46)


asyncio.run(main())
```

```python
import aiohttp
from froeling_lambdatronic import FroelingConnectClient, OperatingMode

async with aiohttp.ClientSession() as session:
    cloud = FroelingConnectClient(session, "user@example.com", "secret")
    facility = (await cloud.get_facilities())[0]
    await cloud.set_operating_mode(facility.facility_id, OperatingMode.DOMESTIC_HOT_WATER)
```

Writing is validated before sending (writable register, range, step) and verified by reading the
register back. The gateway must not be polled by other clients at the same time.

## Development

```bash
uv sync
uv run ruff check && uv run mypy && uv run pytest
```

Register documentation: Fröling "Kommunikationsprotokoll ModBus Lambdatronic 3200", B1200522.
