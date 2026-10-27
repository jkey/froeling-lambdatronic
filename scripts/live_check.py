"""Read-only check against the real boiler, plus one no-op write (same value)."""

import asyncio
import sys

from froeling_lambdatronic import LambdatronicClient, heating_circuit, system_state_text


async def main(host: str) -> None:
    async with LambdatronicClient(host, slave=2) as boiler:
        equipment = await boiler.detect_equipment()
        print("Ausstattung:", equipment)
        values = await boiler.read_values(equipment)
        state = system_state_text(values.system_state) if values.system_state is not None else "?"
        print("Anlage:", state, "| Kessel:", values.boiler_temperature, "°C")
        print("Außen:", values.outside_temperature, "| Fehler:", values.errors)
        parameters = await boiler.read_parameters(equipment)
        hc2 = parameters.heating_circuits[2]
        print("HK2 Heizgrenze Absenk:", hc2.heating_limit_setback)
        assert hc2.heating_limit_setback is not None
        read_back = await boiler.write(heating_circuit(2).heating_limit_setback, hc2.heating_limit_setback)
        print("No-op-Schreiben ok, zurückgelesen:", read_back)


asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "192.168.1.50"))
