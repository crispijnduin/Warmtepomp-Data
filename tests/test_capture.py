"""Integratietests: de hele keten tegen een nep-Home Assistant."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import TOKEN, FakeHA
from warmtepomp.backfill import backfill
from warmtepomp.collector import Collector
from warmtepomp.config import Config
from warmtepomp.discovery import discover
from warmtepomp.ha_client import HAAuthError, HAClient, open_client
from warmtepomp.store import Store


async def _client(server: FakeHA) -> HAClient:
    client = HAClient(f"{server.url}/api", f"ws://127.0.0.1:{server.port}/api/websocket", TOKEN)
    await client.connect()
    return client


async def test_connect_and_read_config(fake_ha: FakeHA, config: Config) -> None:
    async with open_client(config) as client:
        assert client.ha_version == "2026.11.0"
        info = await client.api_status()
        assert info["location_name"] == "Home"


async def test_bad_token_is_reported_clearly(fake_ha: FakeHA, config: Config) -> None:
    broken = config.with_overrides(ha_token="fout")
    with pytest.raises(HAAuthError, match="weigert het token"):
        async with open_client(broken):
            pass


async def test_discovery_picks_device_siblings_and_skips_others(
    fake_ha: FakeHA, config: Config
) -> None:
    async with open_client(config) as client:
        found = await discover(config, client)
    ids = set(found.sorted_ids())
    # Alles van het warmtepomp-device, inclusief de sensor van een ander platform.
    assert "sensor.flint_cop" in ids
    assert "sensor.flint_room_temperature" in ids
    assert "binary_sensor.flint_water_pump" in ids
    # Uitgeschakelde entiteiten en andere apparaten blijven buiten.
    assert "sensor.flint_disabled" not in ids
    assert "sensor.keukenboiler_power" not in ids
    assert found.meta["sensor.flint_input_power"].unit == "W"
    assert found.meta["sensor.flint_cop"].device_name == "Flint P40 heat pump"


async def test_exclude_pattern_is_honoured(fake_ha: FakeHA, config: Config) -> None:
    narrowed = config.with_overrides(exclude_entities=("binary_sensor.*",))
    async with open_client(narrowed) as client:
        found = await discover(narrowed, client)
    assert not [e for e in found.sorted_ids() if e.startswith("binary_sensor.")]


async def test_extra_entities_outside_the_device(fake_ha: FakeHA, config: Config) -> None:
    widened = config.with_overrides(extra_entities=("sensor.keukenboiler_power",))
    async with open_client(widened) as client:
        found = await discover(widened, client)
    assert "sensor.keukenboiler_power" in found.sorted_ids()


async def test_live_capture_records_every_change(fake_ha: FakeHA, config: Config) -> None:
    """De kern: elke statewijziging moet in de database belanden."""
    with Store(config.db_path) as store:
        collector = Collector(config, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: len(collector.targets) > 0)

        base = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
        for i in range(5):
            await fake_ha.push_state(
                "sensor.flint_input_power", str(1000 + i * 10), base + timedelta(seconds=i * 30)
            )
        await fake_ha.push_state("sensor.flint_cop", "4.81", base + timedelta(minutes=3))
        # Een entiteit buiten de selectie mag niet worden vastgelegd.
        await fake_ha.push_state("sensor.keukenboiler_power", "99", base)

        await _wait_until(lambda: _count(store, "sensor.flint_input_power") >= 5)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

        rows = store.conn.execute(
            """SELECT ts, value_num FROM readings
               WHERE entity_id = 'sensor.flint_input_power' AND source = 'event'
               ORDER BY ts"""
        ).fetchall()
        assert [r["value_num"] for r in rows] == [1000, 1010, 1020, 1030, 1040]
        assert _count(store, "sensor.flint_cop") >= 1
        assert _count(store, "sensor.keukenboiler_power") == 0


async def test_binary_and_enum_states_are_usable(fake_ha: FakeHA, config: Config) -> None:
    """binary_sensors worden 1/0 en de ruwe tekst blijft bewaard."""
    with Store(config.db_path) as store:
        collector = Collector(config, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: len(collector.targets) > 0)
        await fake_ha.push_state("binary_sensor.flint_water_pump", "off")
        await _wait_until(lambda: _count(store, "binary_sensor.flint_water_pump") >= 2)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

        rows = store.conn.execute(
            """SELECT value_num, value_text FROM readings
               WHERE entity_id = 'binary_sensor.flint_water_pump' ORDER BY ts"""
        ).fetchall()
        values = {(r["value_num"], r["value_text"]) for r in rows}
        assert (1.0, "on") in values
        assert (0.0, "off") in values


async def test_unavailable_keeps_text_but_no_number(fake_ha: FakeHA, config: Config) -> None:
    with Store(config.db_path) as store:
        collector = Collector(config, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: len(collector.targets) > 0)
        await fake_ha.push_state("sensor.flint_cop", "unavailable")
        await _wait_until(lambda: _count(store, "sensor.flint_cop") >= 2)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

        row = store.conn.execute(
            """SELECT value_num, value_text FROM readings
               WHERE entity_id = 'sensor.flint_cop' ORDER BY ts DESC LIMIT 1"""
        ).fetchone()
        assert row["value_text"] == "unavailable"
        assert row["value_num"] is None


async def test_snapshot_catches_what_events_missed(fake_ha: FakeHA, config: Config) -> None:
    """Een state die zonder event verandert komt via de snapshot toch binnen."""
    with Store(config.db_path) as store:
        collector = Collector(config, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: collector.stats.snapshots >= 1)

        # Wijzig de state stil: geen event, alleen de nieuwe waarde bij get_states.
        fake_ha.states["sensor.flint_outside_temperature"] = {
            "entity_id": "sensor.flint_outside_temperature",
            "state": "9.9",
            "attributes": {"unit_of_measurement": "°C"},
            "last_updated": "2026-11-12T16:00:00+00:00",
            "last_changed": "2026-11-12T16:00:00+00:00",
        }
        await _wait_until(
            lambda: store.conn.execute(
                """SELECT COUNT(*) AS n FROM readings
                   WHERE entity_id = 'sensor.flint_outside_temperature' AND value_num = 9.9"""
            ).fetchone()["n"]
            > 0,
            timeout=8,
        )
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)
        assert collector.stats.snapshots >= 2


async def test_reconnects_and_marks_the_gap(fake_ha: FakeHA, config: Config) -> None:
    """Na verbindingsverlies: gat gemarkeerd, daarna weer data vastleggen."""
    with Store(config.db_path) as store:
        collector = Collector(config, store)
        task = asyncio.create_task(collector.run(max_cycles=3))
        await _wait_until(lambda: len(collector.targets) > 0)

        await fake_ha.drop_connections()
        await _wait_until(lambda: collector.stats.reconnects >= 1, timeout=8)
        # Tijdens de onderbreking is een gat geopend.
        assert store.conn.execute("SELECT COUNT(*) AS n FROM gaps").fetchone()["n"] >= 1

        await _wait_until(lambda: len(fake_ha.subscriptions) > 0, timeout=8)
        await fake_ha.push_state("sensor.flint_cop", "5.55")
        await _wait_until(
            lambda: store.conn.execute(
                "SELECT COUNT(*) AS n FROM readings WHERE value_num = 5.55"
            ).fetchone()["n"]
            > 0,
            timeout=8,
        )
        collector.request_stop()
        await asyncio.wait_for(task, timeout=15)

        # Het gat is weer gesloten toen de verbinding terug was.
        assert store.stats()["open_gaps"] == 0


async def test_new_sensor_is_picked_up_automatically(fake_ha: FakeHA, config: Config) -> None:
    """Een sensor die later verschijnt gaat vanzelf mee in de capture."""
    from tests.conftest import DEVICE_ID, ENTITY_REGISTRY

    with Store(config.db_path) as store:
        collector = Collector(config, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: len(collector.targets) > 0)
        assert "sensor.flint_brand_new" not in collector.targets

        ENTITY_REGISTRY.append(
            {
                "entity_id": "sensor.flint_brand_new",
                "platform": "weheat",
                "device_id": DEVICE_ID,
                "original_name": "Nieuwe sensor",
            }
        )
        fake_ha.states["sensor.flint_brand_new"] = {
            "entity_id": "sensor.flint_brand_new",
            "state": "1.0",
            "attributes": {},
            "last_updated": "2026-11-12T16:00:00+00:00",
            "last_changed": "2026-11-12T16:00:00+00:00",
        }
        try:
            await _wait_until(
                lambda: "sensor.flint_brand_new" in collector.targets, timeout=8
            )
            await fake_ha.push_state("sensor.flint_brand_new", "2.0")
            await _wait_until(lambda: _count(store, "sensor.flint_brand_new") >= 1, timeout=8)
        finally:
            ENTITY_REGISTRY[:] = [
                e for e in ENTITY_REGISTRY if e["entity_id"] != "sensor.flint_brand_new"
            ]
            collector.request_stop()
            await asyncio.wait_for(task, timeout=10)


async def test_backfill_imports_statistics_and_history(
    fake_ha: FakeHA, config: Config
) -> None:
    with Store(config.db_path) as store:
        async with open_client(config) as client:
            result = await backfill(config, client, store, days=2)
        assert result.statistics_rows > 0
        assert result.reading_rows > 0
        assert not result.errors

        periods = {
            row["period"]
            for row in store.conn.execute("SELECT DISTINCT period FROM statistics")
        }
        assert {"5minute", "hour", "day", "month"} <= periods
        # Historie wordt als zodanig gemarkeerd, zodat herkomst traceerbaar is.
        assert (
            store.conn.execute(
                "SELECT COUNT(*) AS n FROM readings WHERE source = 'history'"
            ).fetchone()["n"]
            > 0
        )

        # Nog een keer backfillen levert geen duplicaten op.
        before = store.stats()["readings"]
        async with open_client(config) as client:
            await backfill(config, client, store, days=2)
        assert store.stats()["readings"] == before


def _count(store: Store, entity_id: str) -> int:
    return int(
        store.conn.execute(
            "SELECT COUNT(*) AS n FROM readings WHERE entity_id = ?", (entity_id,)
        ).fetchone()["n"]
    )


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    """Wacht tot `predicate()` waar is, anders faal met een duidelijke melding."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"voorwaarde niet gehaald binnen {timeout}s")
