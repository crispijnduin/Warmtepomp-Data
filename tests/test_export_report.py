"""Tests voor export, analyse en de commandoregel."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from warmtepomp import cli
from warmtepomp.analyse import cop_vs_outside_temperature, daily_energy, seasonal_cop
from warmtepomp.config import Config, ConfigError, load_config
from warmtepomp.export import (
    entity_metadata_csv,
    export_all,
    export_readings,
    export_statistics,
    export_wide,
)
from warmtepomp.store import EntityMeta, Reading, Store

BASE = datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def filled_store(tmp_path: Path) -> Store:
    """Een database met drie dagen plausibele warmtepompdata."""
    store = Store(tmp_path / "wp.sqlite")
    store.upsert_entity(
        EntityMeta("sensor.flint_cop", "COP", state_class="measurement")
    )
    store.upsert_entity(
        EntityMeta("sensor.flint_input_power", "Input power", unit="W", device_class="power")
    )
    store.upsert_entity(
        EntityMeta(
            "sensor.flint_electricity_used",
            "Electricity used",
            unit="kWh",
            device_class="energy",
        )
    )
    store.upsert_entity(
        EntityMeta(
            "sensor.flint_total_energy_output",
            "Total energy output",
            unit="kWh",
            device_class="energy",
        )
    )
    store.upsert_entity(
        EntityMeta("sensor.flint_outside_temperature", "Outside", unit="°C")
    )
    store.upsert_entity(EntityMeta("sensor.flint_mode", "Mode", device_class="enum"))

    readings = []
    for minute in range(0, 3 * 24 * 60, 10):
        ts = BASE + timedelta(minutes=minute)
        readings.append(Reading.from_state("sensor.flint_cop", f"{4 + (minute % 60) / 100:.2f}", ts))
        readings.append(Reading.from_state("sensor.flint_input_power", str(1000 + minute % 500), ts))
        readings.append(
            Reading.from_state("sensor.flint_outside_temperature", f"{5 + (minute % 120) / 10:.1f}", ts)
        )
        readings.append(Reading.from_state("sensor.flint_mode", "heating", ts))
    store.add_readings(readings)

    # Dag-statistieken voor de energiebalans.
    for day in range(3):
        start = BASE + timedelta(days=day)
        store.add_statistics(
            "sensor.flint_electricity_used", "day", [{"start": start, "change": 10.0 + day}]
        )
        store.add_statistics(
            "sensor.flint_total_energy_output", "day", [{"start": start, "change": 35.0 + day * 4}]
        )
    # Uur-statistieken voor de COP-curve.
    for hour in range(48):
        start = BASE + timedelta(hours=hour)
        store.add_statistics(
            "sensor.flint_cop", "hour", [{"start": start, "mean": 3.0 + (hour % 12) * 0.2}]
        )
        store.add_statistics(
            "sensor.flint_outside_temperature",
            "hour",
            [{"start": start, "mean": -2.0 + (hour % 12) * 1.5}],
        )
    yield store
    store.close()


def test_export_readings_is_complete_and_readable(filled_store: Store, tmp_path: Path) -> None:
    result = export_readings(filled_store, tmp_path / "out")
    assert result.rows == filled_store.stats()["readings"]
    with (tmp_path / "out" / "readings.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == result.rows
    assert set(rows[0]) == {
        "ts",
        "entity_id",
        "friendly_name",
        "value_num",
        "value_text",
        "unit",
        "device_class",
        "source",
    }
    # Chronologisch gesorteerd.
    assert [r["ts"] for r in rows] == sorted(r["ts"] for r in rows)
    # Eenheden komen uit de metadata mee.
    power = next(r for r in rows if r["entity_id"] == "sensor.flint_input_power")
    assert power["unit"] == "W"


def test_export_wide_pivots_and_carries_last_value(filled_store: Store, tmp_path: Path) -> None:
    result = export_wide(filled_store, tmp_path / "out", resample="hour")
    with (tmp_path / "out" / "wide_hour.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert result.rows == len(rows) == 72  # drie dagen aan uren
    assert "sensor.flint_cop" in rows[0]
    # Elke rij heeft voor elke numerieke entiteit een waarde (laatst bekende).
    assert all(row["sensor.flint_input_power"] for row in rows)
    # De tekstuele enum-sensor hoort niet in de numerieke brede tabel.
    assert "sensor.flint_mode" not in rows[0]


def test_export_wide_rejects_unknown_resample(filled_store: Store, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="onbekende resample"):
        export_wide(filled_store, tmp_path / "out", resample="elke-maandag")


def test_export_statistics_and_entities(filled_store: Store, tmp_path: Path) -> None:
    stats = export_statistics(filled_store, tmp_path / "out")
    assert stats.rows == filled_store.stats()["statistics"]
    meta = entity_metadata_csv(filled_store, tmp_path / "out")
    with (tmp_path / "out" / "entities.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert meta.rows == len(rows) == 6
    cop = next(r for r in rows if r["entity_id"] == "sensor.flint_cop")
    assert int(cop["readings"]) > 0


def test_export_all_writes_every_file(filled_store: Store, tmp_path: Path) -> None:
    results = export_all(filled_store, tmp_path / "out", resample="15min")
    produced = {p.name for r in results.values() for p in r.files}
    assert produced == {"readings.csv", "wide_15min.csv", "statistics.csv"}
    for path in (tmp_path / "out").iterdir():
        assert path.stat().st_size > 0


def test_daily_energy_and_scop(filled_store: Store) -> None:
    rows = daily_energy(filled_store)
    assert len(rows) == 3
    assert rows[0].electricity_kwh == 10.0
    assert rows[0].output_kwh == 35.0
    assert rows[0].cop == pytest.approx(3.5)
    total = seasonal_cop(filled_store)
    assert total["electricity_kwh"] == pytest.approx(33.0)   # 10 + 11 + 12
    assert total["output_kwh"] == pytest.approx(117.0)       # 35 + 39 + 43
    assert total["scop"] == pytest.approx(117.0 / 33.0, abs=1e-3)
    assert total["days"] == 3


def test_cop_curve_buckets_by_outside_temperature(filled_store: Store) -> None:
    curve = cop_vs_outside_temperature(filled_store, bucket=2.0)
    assert curve, "er zijn uur-statistieken, dus een curve hoort te lukken"
    assert all(b["hours"] > 0 for b in curve)
    assert all(b["cop_mean"] > 0 for b in curve)
    lows = [b["outside_temp_from"] for b in curve]
    assert lows == sorted(lows)


def test_daily_energy_falls_back_to_meter_readings(tmp_path: Path) -> None:
    """Zonder dag-statistieken wordt het verbruik uit de meterstand afgeleid."""
    with Store(tmp_path / "wp.sqlite") as store:
        store.upsert_entity(EntityMeta("sensor.x_electricity_used", "Stroom", unit="kWh"))
        store.upsert_entity(EntityMeta("sensor.x_total_energy_output", "Warmte", unit="kWh"))
        for hour in range(6):
            ts = BASE + timedelta(hours=hour)
            store.add_readings(
                [
                    Reading.from_state("sensor.x_electricity_used", str(100 + hour), ts),
                    Reading.from_state("sensor.x_total_energy_output", str(300 + hour * 3), ts),
                ]
            )
        rows = daily_energy(store)
        assert len(rows) == 1
        assert rows[0].electricity_kwh == pytest.approx(5.0)
        assert rows[0].output_kwh == pytest.approx(15.0)
        assert rows[0].cop == pytest.approx(3.0)


# --------------------------------------------------------------------- config


def test_config_rejects_empty_selection() -> None:
    with pytest.raises(ConfigError, match="minstens één"):
        Config(ha_url="http://x", ha_token="t", integrations=(), devices=(), extra_entities=())


def test_config_env_beats_file(tmp_path: Path) -> None:
    path = tmp_path / "warmtepomp.yaml"
    path.write_text(
        "ha_url: http://uit-bestand:8123\nintegrations: [weheat]\nsnapshot_interval: 60\n",
        encoding="utf-8",
    )
    cfg = load_config(path, environ={"WARMTEPOMP_HA_URL": "http://uit-env:8123"})
    assert cfg.ha_url == "http://uit-env:8123"
    assert cfg.snapshot_interval == 60.0
    assert cfg.source_path == path


def test_config_reads_addon_options_json(tmp_path: Path) -> None:
    """Een HA add-on levert zijn instellingen als /data/options.json."""
    path = tmp_path / "options.json"
    path.write_text(
        json.dumps(
            {
                "ha_url": "http://supervisor/core",
                "integrations": "weheat,quatt",
                "store_attributes": False,
                "log_level": "DEBUG",
                "unknown_addon_key": "negeer mij",
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config(path, environ={"SUPERVISOR_TOKEN": "supertoken"})
    assert cfg.integrations == ("weheat", "quatt")
    assert cfg.store_attributes is False
    assert cfg.ha_token == "supertoken"
    assert cfg.log_level == "DEBUG"


def test_explicit_token_beats_supervisor_token(tmp_path: Path) -> None:
    cfg = load_config(
        None,
        environ={
            "WARMTEPOMP_HA_URL": "http://x:8123",
            "SUPERVISOR_TOKEN": "super",
            "WARMTEPOMP_HA_TOKEN": "eigen",
        },
    )
    assert cfg.ha_token == "eigen"


def test_ws_url_upgrades_scheme() -> None:
    assert Config(ha_url="https://ha.example", ha_token="t").ws_url.startswith("wss://")
    assert Config(ha_url="http://ha.local:8123", ha_token="t").ws_url == (
        "ws://ha.local:8123/api/websocket"
    )


# ------------------------------------------------------------------------ cli


def test_cli_status_export_report_and_query(
    filled_store: Store, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = str(filled_store.path)
    cfg = tmp_path / "warmtepomp.yaml"
    cfg.write_text(f"ha_url: http://x:8123\nha_token: t\ndb_path: {db}\n", encoding="utf-8")
    common = ["--config", str(cfg)]

    assert cli.main([*common, "status", "--per-entity"]) == 0
    out = capsys.readouterr().out
    assert "Entiteiten 6" in out
    assert "sensor.flint_cop" in out

    assert cli.main([*common, "--json", "report"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"]["days"] == 3
    assert payload["total"]["scop"] > 3

    assert cli.main([*common, "export", "--out", str(tmp_path / "exp"), "--resample", "hour"]) == 0
    capsys.readouterr()
    assert (tmp_path / "exp" / "readings.csv").is_file()
    assert (tmp_path / "exp" / "wide_hour.csv").is_file()
    assert (tmp_path / "exp" / "entities.csv").is_file()

    assert cli.main([*common, "query", "SELECT COUNT(*) AS n FROM readings"]) == 0
    assert "n" in capsys.readouterr().out


def test_cli_query_refuses_writes(filled_store: Store, tmp_path: Path) -> None:
    cfg = tmp_path / "warmtepomp.yaml"
    cfg.write_text(
        f"ha_url: http://x:8123\nha_token: t\ndb_path: {filled_store.path}\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="Alleen SELECT"):
        cli.main(["--config", str(cfg), "query", "DELETE FROM readings"])
    assert filled_store.stats()["readings"] > 0


def test_cli_status_without_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "warmtepomp.yaml"
    cfg.write_text(
        f"ha_url: http://x:8123\nha_token: t\ndb_path: {tmp_path / 'leeg.sqlite'}\n",
        encoding="utf-8",
    )
    assert cli.main(["--config", str(cfg), "status"]) == 1
    assert "Nog geen database" in capsys.readouterr().out


def test_ws_url_handles_supervisor_proxy() -> None:
    """In een add-on loopt alles via de Supervisor op een ander pad."""
    cfg = Config(ha_url="http://supervisor/core", ha_token="t")
    assert cfg.ws_url == "ws://supervisor/core/websocket"
    assert cfg.rest_url == "http://supervisor/core/api"


def test_explicit_ws_url_wins() -> None:
    cfg = Config(
        ha_url="https://ha.example", ha_token="t", ha_ws_url="wss://ha.example/extern/ws"
    )
    assert cfg.ws_url == "wss://ha.example/extern/ws"
