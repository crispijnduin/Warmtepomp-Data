"""Attributen vastleggen als eigen meetreeks.

Bij een thermostaat is de state 'heat' en staat de kamertemperatuur in het
attribuut current_temperature. Home Assistant bewaart daar geen statistiek
van, dus die reeks is na de bewaartermijn verdwenen. Deze tests controleren
dat hij bij ons wel bewaard blijft, en bruikbaar is.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.conftest import FakeHA
from warmtepomp.collector import Collector, attribute_series_id
from warmtepomp.config import Config
from warmtepomp.store import Store

WOONKAMER = "climate.woonkamer"
TEMP_SERIES = attribute_series_id(WOONKAMER, "current_temperature")
DOEL_SERIES = attribute_series_id(WOONKAMER, "temperature")


@pytest.fixture
def with_thermostat(config: Config) -> Config:
    return config.with_overrides(integrations=("weheat", "evohome"))


async def test_room_temperature_becomes_a_normal_series(
    fake_ha: FakeHA, with_thermostat: Config
) -> None:
    """De kamertemperatuur moet net zo opvraagbaar zijn als elke sensor."""
    with Store(with_thermostat.db_path) as store:
        collector = Collector(with_thermostat, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: WOONKAMER in collector.targets)
        await _wait_until(lambda: _count(store, TEMP_SERIES) >= 1, timeout=8)

        await fake_ha.push_state(WOONKAMER, "heat", current_temperature=18.4)
        await _wait_until(lambda: _count(store, TEMP_SERIES) >= 2, timeout=8)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

        rows = store.conn.execute(
            "SELECT value_num FROM readings WHERE entity_id = ? ORDER BY ts",
            (TEMP_SERIES,),
        ).fetchall()
        assert [r["value_num"] for r in rows] == [17.9, 18.4]

        # Het setpoint komt als eigen reeks mee.
        assert _count(store, DOEL_SERIES) >= 1


async def test_series_gets_usable_metadata(fake_ha: FakeHA, with_thermostat: Config) -> None:
    """Zonder eenheid en state_class is de reeks niet te plotten of te exporteren."""
    with Store(with_thermostat.db_path) as store:
        collector = Collector(with_thermostat, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: _count(store, TEMP_SERIES) >= 1, timeout=8)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

        row = store.conn.execute(
            "SELECT * FROM entities WHERE entity_id = ?", (TEMP_SERIES,)
        ).fetchone()
        assert row is not None
        assert row["unit"] == "°C"
        assert row["device_class"] == "temperature"
        assert row["state_class"] == "measurement"
        assert "Woonkamer" in row["friendly_name"]
        assert "current_temperature" in row["friendly_name"]


async def test_the_state_itself_is_still_captured(
    fake_ha: FakeHA, with_thermostat: Config
) -> None:
    """De reeks komt erbij, hij vervangt de gewone state niet."""
    with Store(with_thermostat.db_path) as store:
        collector = Collector(with_thermostat, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: _count(store, WOONKAMER) >= 1, timeout=8)
        await fake_ha.push_state(WOONKAMER, "off", current_temperature=16.0)
        await _wait_until(lambda: _count(store, WOONKAMER) >= 2, timeout=8)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

        states = [
            r["value_text"]
            for r in store.conn.execute(
                "SELECT value_text FROM readings WHERE entity_id = ? ORDER BY ts",
                (WOONKAMER,),
            )
        ]
        assert states == ["heat", "off"]


async def test_room_temperature_lines_up_with_the_heat_pump(
    fake_ha: FakeHA, with_thermostat: Config
) -> None:
    """Het punt van de hele exercitie: comfort naast aanvoertemperatuur zetten."""
    from warmtepomp.export import export_wide

    with Store(with_thermostat.db_path) as store:
        collector = Collector(with_thermostat, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: _count(store, TEMP_SERIES) >= 1, timeout=8)
        await fake_ha.push_state("sensor.flint_outside_temperature", "4.2")
        await fake_ha.push_state(WOONKAMER, "heat", current_temperature=18.1)
        await _wait_until(lambda: _count(store, TEMP_SERIES) >= 2, timeout=8)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

        result = export_wide(store, with_thermostat.export_dir, resample="raw")
        assert result.rows > 0
        header = (result.files[0]).read_text(encoding="utf-8").splitlines()[0]
        # Kamertemperatuur en warmtepompdata staan als kolommen naast elkaar.
        assert TEMP_SERIES in header
        assert "sensor.flint_outside_temperature" in header


async def test_non_numeric_attributes_are_not_turned_into_series(
    fake_ha: FakeHA, with_thermostat: Config
) -> None:
    """preset_mode is tekst en hoort geen meetreeks te worden."""
    with Store(with_thermostat.db_path) as store:
        collector = Collector(with_thermostat, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: _count(store, TEMP_SERIES) >= 1, timeout=8)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

        series = [
            r["entity_id"]
            for r in store.conn.execute(
                "SELECT entity_id FROM entities WHERE entity_id LIKE ?", (f"{WOONKAMER}#%",)
            )
        ]
        assert attribute_series_id(WOONKAMER, "preset_mode") not in series
        assert TEMP_SERIES in series


async def test_feature_can_be_switched_off(fake_ha: FakeHA, with_thermostat: Config) -> None:
    plain = with_thermostat.with_overrides(attribute_series=())
    with Store(plain.db_path) as store:
        collector = Collector(plain, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: _count(store, WOONKAMER) >= 1, timeout=8)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)
        assert _count(store, TEMP_SERIES) == 0


def _count(store: Store, entity_id: str) -> int:
    return int(
        store.conn.execute(
            "SELECT COUNT(*) AS n FROM readings WHERE entity_id = ?", (entity_id,)
        ).fetchone()["n"]
    )


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"voorwaarde niet gehaald binnen {timeout}s")


async def test_backfill_rescues_historical_room_temperatures(
    fake_ha: FakeHA, with_thermostat: Config
) -> None:
    """De kamertemperatuur van de afgelopen dagen staat nog in Home Assistant.

    Die is daar binnenkort weg, want er wordt geen statistiek van bewaard.
    De backfill hoort hem alsnog binnen te halen - met attributen, want
    zonder attributen levert de history-API alleen 'heat' en 'off'.
    """
    from warmtepomp.backfill import backfill
    from warmtepomp.ha_client import open_client

    with Store(with_thermostat.db_path) as store:
        async with open_client(with_thermostat) as client:
            result = await backfill(with_thermostat, client, store, days=2)
        assert not result.errors

        rows = store.conn.execute(
            """SELECT value_num FROM readings
               WHERE entity_id = ? AND source = 'history' ORDER BY ts""",
            (TEMP_SERIES,),
        ).fetchall()
        assert [r["value_num"] for r in rows] == [16.0, 17.0, 18.0]

        # De gewone sensoren zijn zonder attributen opgehaald; die hoeven dat
        # niet en het antwoord zou anders onnodig groot worden.
        assert _count(store, "sensor.flint_cop") > 0


async def test_backfill_leaves_plain_sensors_alone(
    fake_ha: FakeHA, with_thermostat: Config
) -> None:
    """Sensoren zonder gevolgde attributen krijgen geen pseudo-reeksen."""
    from warmtepomp.backfill import backfill
    from warmtepomp.ha_client import open_client

    with Store(with_thermostat.db_path) as store:
        async with open_client(with_thermostat) as client:
            await backfill(with_thermostat, client, store, days=2)
        extra = store.conn.execute(
            "SELECT entity_id FROM entities WHERE entity_id LIKE 'sensor.%#%'"
        ).fetchall()
        assert not extra
