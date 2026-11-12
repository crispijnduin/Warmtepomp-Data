"""Tests voor de brug: samenvatting omhoog, vragen omlaag.

Het punt van deze brug is dat de inhoud van de database bereikbaar is voor
wie alleen de Home Assistant-API heeft. Deze tests draaien daarom tegen de
nep-Home Assistant en kijken naar wat daar werkelijk terechtkomt.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from tests.conftest import FakeHA
from warmtepomp.bridge import (
    QUERY_RESULT_ENTITY,
    QueryRefused,
    build_digest,
    count_compressor_starts,
    known_modes,
    minutes_per_mode,
    publish_digest,
    run_query,
)
from warmtepomp.collector import Collector
from warmtepomp.config import Config
from warmtepomp.ha_client import open_client
from warmtepomp.store import EntityMeta, Reading, Store
from warmtepomp.util import utcnow

PREFIX = "sensor.home_flint_p40_heat_pump"


@pytest.fixture
def pumping_store(tmp_path) -> Store:
    """Een pomp die het afgelopen uur vier keer kort heeft aangeslagen."""
    store = Store(tmp_path / "wp.sqlite")
    store.upsert_entity(EntityMeta(f"{PREFIX}_input_power", "Input power", unit="W"))
    store.upsert_entity(EntityMeta(f"{PREFIX}", "Bedrijfsmodus", device_class="enum"))

    now = utcnow()
    # Vóór het tijdvenster stond de pomp stil. Dat is belangrijk: anders zou
    # de eerste meting binnen het venster als start kunnen meetellen terwijl
    # de pomp al draaide.
    readings = [
        Reading.from_state(f"{PREFIX}_input_power", "20", now - timedelta(minutes=70))
    ]
    # Vier korte cycli binnen het uur: telkens 5 minuten aan, dan uit.
    for minutes_ago in (55, 42, 29, 16):
        base = now - timedelta(minutes=minutes_ago)
        readings.append(Reading.from_state(f"{PREFIX}_input_power", "1200", base))
        readings.append(
            Reading.from_state(f"{PREFIX}_input_power", "1150", base + timedelta(minutes=3))
        )
        readings.append(
            Reading.from_state(f"{PREFIX}_input_power", "20", base + timedelta(minutes=5))
        )
    store.add_readings(readings)

    # Bedrijfsmodus: 30 minuten verwarmen, 10 ontdooien, de rest standby.
    store.add_readings(
        [
            Reading.from_state(f"{PREFIX}", "heating", now - timedelta(minutes=60)),
            Reading.from_state(f"{PREFIX}", "defrosting", now - timedelta(minutes=30)),
            Reading.from_state(f"{PREFIX}", "standby", now - timedelta(minutes=20)),
        ]
    )
    yield store
    store.close()


# ------------------------------------------------------------------- omhoog


def test_compressor_starts_reveal_cycling(pumping_store: Store) -> None:
    """Vier korte cycli moeten als vier starts worden geteld.

    Dit is precies wat in een uurgemiddelde verdwijnt.
    """
    assert count_compressor_starts(pumping_store, hours=1.0) == 4


def test_a_pump_already_running_is_not_counted_as_a_start(tmp_path) -> None:
    """Draaide de pomp het uur al in, dan is dat geen nieuwe start.

    Zonder deze correctie zou elk uur minstens één start tellen, ook bij een
    pomp die rustig doordraait - precies het tegenovergestelde van pendelen.
    """
    with Store(tmp_path / "wp.sqlite") as store:
        store.upsert_entity(EntityMeta(f"{PREFIX}_input_power", "IP", unit="W"))
        now = utcnow()
        store.add_readings(
            [
                # Al aan voordat het venster begon, en blijft gewoon aan.
                Reading.from_state(f"{PREFIX}_input_power", "1200", now - timedelta(hours=3)),
                Reading.from_state(f"{PREFIX}_input_power", "1180", now - timedelta(minutes=40)),
                Reading.from_state(f"{PREFIX}_input_power", "1210", now - timedelta(minutes=10)),
            ]
        )
        assert count_compressor_starts(store, hours=1.0) == 0


def test_an_unchanged_mode_still_counts(tmp_path) -> None:
    """Een modus die al uren ongewijzigd is, moet het hele uur vullen.

    De meting staat dan buiten het venster; zonder terugkijken zou de
    tijdsverdeling leeg blijven juist als de pomp stabiel draait.
    """
    with Store(tmp_path / "wp.sqlite") as store:
        store.upsert_entity(EntityMeta(f"{PREFIX}", "Modus", device_class="enum"))
        store.add_readings(
            [Reading.from_state(f"{PREFIX}", "heating", utcnow() - timedelta(hours=5))]
        )
        modes = minutes_per_mode(store, hours=1.0)
        assert modes == pytest.approx({"heating": 60.0}, abs=0.2)


def test_mode_entity_is_found_without_knowing_the_brand(tmp_path) -> None:
    """De modussensor wordt op inhoud herkend, niet op merknaam."""
    with Store(tmp_path / "wp.sqlite") as store:
        store.upsert_entity(EntityMeta("sensor.quatt_cic_mode", "Mode"))
        store.upsert_entity(EntityMeta("sensor.quatt_power", "Power", unit="W"))
        now = utcnow()
        store.add_readings(
            [
                Reading.from_state("sensor.quatt_cic_mode", "heating", now - timedelta(minutes=45)),
                Reading.from_state("sensor.quatt_cic_mode", "standby", now - timedelta(minutes=15)),
            ]
        )
        modes = minutes_per_mode(store, hours=1.0)
        assert modes["heating"] == pytest.approx(30, abs=0.5)
        assert modes["standby"] == pytest.approx(15, abs=0.5)


def test_compressor_starts_none_without_data(tmp_path) -> None:
    with Store(tmp_path / "leeg.sqlite") as store:
        assert count_compressor_starts(store) is None


def test_minutes_per_mode_sums_to_the_window(pumping_store: Store) -> None:
    modes = minutes_per_mode(pumping_store, hours=1.0)
    assert set(modes) == {"heating", "defrosting", "standby"}
    assert modes["heating"] == pytest.approx(30, abs=0.5)
    assert modes["defrosting"] == pytest.approx(10, abs=0.5)
    assert modes["standby"] == pytest.approx(20, abs=0.5)
    assert sum(modes.values()) == pytest.approx(60, abs=1.0)


def test_digest_entities_are_recordable(pumping_store: Store) -> None:
    """Zonder state_class bewaart de recorder geen statistiek.

    Dat is het hele punt: de afgeleiden moeten over een jaar nog opvraagbaar
    zijn, ook als de onderliggende metingen zijn opgeruimd.
    """
    entities = {e.entity_id: e for e in build_digest(pumping_store)}
    starts = entities["sensor.warmtepomp_compressor_starts_1h"]
    assert starts.state == "4"
    assert starts.attributes["state_class"] == "measurement"
    assert starts.attributes["unit_of_measurement"] == "starts/h"

    heating = entities["sensor.warmtepomp_minutes_heating_1h"]
    assert heating.attributes["state_class"] == "measurement"
    assert float(heating.state) == pytest.approx(30, abs=0.5)

    # De hoofdmodus is tekst en krijgt dus geen state_class, maar wel de
    # volledige verdeling als attribuut.
    dominant = entities["sensor.warmtepomp_mode_last_hour"]
    assert dominant.state == "heating"
    assert "minutes_per_mode" in dominant.attributes

    health = entities["sensor.warmtepomp_capture_readings"]
    assert int(health.state) == pumping_store.stats()["readings"]
    assert health.attributes["lag_seconds"] is not None


def test_mode_names_become_safe_entity_ids(tmp_path) -> None:
    """Een modus met rare tekens mag geen ongeldige entity_id opleveren."""
    with Store(tmp_path / "wp.sqlite") as store:
        store.upsert_entity(EntityMeta(f"{PREFIX}", "Modus", device_class="enum"))
        store.add_readings(
            [Reading.from_state(f"{PREFIX}", "Legionella Prevention!", utcnow() - timedelta(minutes=30))]
        )
        ids = [e.entity_id for e in build_digest(store)]
        assert "sensor.warmtepomp_minutes_legionella_prevention_1h" in ids


async def test_digest_really_lands_in_home_assistant(
    fake_ha: FakeHA, config: Config, pumping_store: Store
) -> None:
    async with open_client(config) as client:
        published = await publish_digest(client, pumping_store)
    assert published >= 4
    assert "sensor.warmtepomp_compressor_starts_1h" in fake_ha.posted
    assert fake_ha.posted["sensor.warmtepomp_compressor_starts_1h"]["state"] == "4"


# ------------------------------------------------------------------- omlaag


def test_query_returns_columns_and_rows(pumping_store: Store) -> None:
    result = run_query(
        pumping_store,
        "SELECT entity_id, COUNT(*) AS n FROM readings GROUP BY entity_id ORDER BY entity_id",
    )
    assert result["columns"] == ["entity_id", "n"]
    assert result["row_count"] == 2
    assert not result["truncated"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM readings",
        "UPDATE readings SET value_num = 0",
        "DROP TABLE readings",
        "SELECT 1; DROP TABLE readings",
        "PRAGMA journal_mode=DELETE",
        "ATTACH DATABASE '/etc/passwd' AS p",
        "INSERT INTO readings VALUES ('x','y',1,'z','w')",
        "",
    ],
)
def test_query_refuses_anything_that_is_not_a_read(pumping_store: Store, sql: str) -> None:
    before = pumping_store.stats()["readings"]
    with pytest.raises(QueryRefused):
        run_query(pumping_store, sql)
    assert pumping_store.stats()["readings"] == before


def test_query_truncates_instead_of_flooding(pumping_store: Store) -> None:
    result = run_query(pumping_store, "SELECT * FROM readings", limit=3)
    assert result["row_count"] == 3
    assert result["truncated"] is True


async def test_question_through_the_logbook_gets_an_answer(
    fake_ha: FakeHA, config: Config
) -> None:
    """De hele keten: logbook.log erin, antwoord als entiteit eruit."""
    with Store(config.db_path) as store:
        collector = Collector(config, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: len(collector.targets) > 0)
        await fake_ha.push_state("sensor.flint_cop", "4.44")
        await _wait_until(lambda: store.stats()["readings"] > 0)

        await fake_ha.fire_logbook_entry(
            "warmtepomp-query",
            "SELECT entity_id, value_num FROM readings WHERE value_num = 4.44",
        )
        await _wait_until(lambda: QUERY_RESULT_ENTITY in fake_ha.posted, timeout=8)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

    answer = fake_ha.posted[QUERY_RESULT_ENTITY]
    assert answer["attributes"]["ok"] is True
    assert answer["attributes"]["columns"] == ["entity_id", "value_num"]
    assert answer["attributes"]["rows"] == [["sensor.flint_cop", 4.44]]
    assert collector.stats.queries_answered == 1


async def test_a_refused_question_reports_why(fake_ha: FakeHA, config: Config) -> None:
    with Store(config.db_path) as store:
        collector = Collector(config, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: len(collector.targets) > 0)
        await fake_ha.fire_logbook_entry("warmtepomp-query", "DELETE FROM readings")
        await _wait_until(lambda: QUERY_RESULT_ENTITY in fake_ha.posted, timeout=8)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)

        answer = fake_ha.posted[QUERY_RESULT_ENTITY]
        assert answer["state"] == "fout"
        assert answer["attributes"]["ok"] is False
        assert "SELECT" in answer["attributes"]["error"]
        # De database is onaangeroerd gebleven.
        assert "readings" in {
            r["name"]
            for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


async def test_other_logbook_entries_are_ignored(fake_ha: FakeHA, config: Config) -> None:
    """Gewone logboekregels mogen geen vraag worden."""
    with Store(config.db_path) as store:
        collector = Collector(config, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: len(collector.targets) > 0)
        await fake_ha.fire_logbook_entry("de voordeur", "is geopend")
        await asyncio.sleep(0.3)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)
    assert QUERY_RESULT_ENTITY not in fake_ha.posted
    assert collector.stats.queries_answered == 0


async def test_queries_can_be_switched_off(fake_ha: FakeHA, config: Config) -> None:
    quiet = config.with_overrides(answer_queries=False, publish_digest=False)
    with Store(quiet.db_path) as store:
        collector = Collector(quiet, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: len(collector.targets) > 0)
        await fake_ha.fire_logbook_entry("warmtepomp-query", "SELECT 1")
        await asyncio.sleep(0.3)
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)
    assert QUERY_RESULT_ENTITY not in fake_ha.posted
    assert not fake_ha.posted


async def test_digest_runs_alongside_capture(fake_ha: FakeHA, config: Config) -> None:
    """De samenvatting mag het vastleggen niet in de weg zitten."""
    busy = config.with_overrides(digest_interval=0.3)
    with Store(busy.db_path) as store:
        collector = Collector(busy, store)
        task = asyncio.create_task(collector.run(max_cycles=1))
        await _wait_until(lambda: "sensor.warmtepomp_capture_readings" in fake_ha.posted,
                          timeout=8)
        for i in range(5):
            await fake_ha.push_state("sensor.flint_input_power", str(900 + i))
        await _wait_until(
            lambda: store.conn.execute(
                "SELECT COUNT(*) AS n FROM readings WHERE entity_id='sensor.flint_input_power'"
            ).fetchone()["n"]
            >= 5
        )
        collector.request_stop()
        await asyncio.wait_for(task, timeout=10)
    assert collector.stats.digests_published > 0


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"voorwaarde niet gehaald binnen {timeout}s")


def test_a_mode_that_left_the_window_is_published_as_zero(pumping_store: Store) -> None:
    """Een modus die dit uur niet voorkwam moet op nul, niet blijven hangen.

    Gebeurde in het echt: ontdooien liep uit het tijdvenster, maar de sensor
    bleef op 0,60 minuten staan terwijl verwarmen al op 60 stond. Samen meer
    dan een uur, dus zichtbaar fout.
    """
    now = utcnow()
    # Alleen nog 'heating' in het laatste uur; 'defrosting' lag daarvoor.
    pumping_store.add_readings(
        [Reading.from_state(f"{PREFIX}", "heating", now - timedelta(minutes=5))]
    )
    entities = {e.entity_id: e for e in build_digest(pumping_store)}
    assert "sensor.warmtepomp_minutes_defrosting_1h" in entities

    totals = {
        e.entity_id: float(e.state)
        for e in entities.values()
        if e.entity_id.startswith("sensor.warmtepomp_minutes_")
    }
    # De optelsom mag nooit meer dan een uur zijn.
    assert sum(totals.values()) == pytest.approx(60.0, abs=0.5), totals


def test_known_modes_come_from_what_was_actually_measured(pumping_store: Store) -> None:
    modes = known_modes(pumping_store)
    assert set(modes) == {"heating", "defrosting", "standby"}


def test_known_modes_is_capped(tmp_path) -> None:
    """Een sensor die onzin meldt mag geen honderden entiteiten opleveren."""
    from warmtepomp.bridge import MAX_MODE_ENTITIES

    with Store(tmp_path / "wp.sqlite") as store:
        store.upsert_entity(EntityMeta("sensor.rare_mode", "Modus", device_class="enum"))
        now = utcnow()
        store.add_readings(
            [
                Reading.from_state("sensor.rare_mode", f"toestand_{i}", now - timedelta(minutes=i))
                for i in range(60)
            ]
        )
        assert len(known_modes(store)) <= MAX_MODE_ENTITIES
