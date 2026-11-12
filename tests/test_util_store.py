"""Tests voor de hulpfuncties en de opslaglaag."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from warmtepomp.store import EntityMeta, Reading, Store
from warmtepomp.util import iso, parse_ts, to_float

TS = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("4.37", 4.37),
        ("13.2", 13.2),
        ("-3.31", -3.31),
        ("0", 0.0),
        ("on", 1.0),
        ("off", 0.0),
        # Enum-toestanden krijgen bewust geen cijfer; ze blijven tekst.
        ("heating", None),
        ("idle", None),
        ("standby", None),
        ("defrosting", None),
        ("unknown", None),
        ("unavailable", None),
        ("", None),
        (None, None),
        ("control_method", None),
        (5128, 5128.0),
        (True, 1.0),
    ],
)
def test_to_float(value: object, expected: float | None) -> None:
    assert to_float(value) == expected  # type: ignore[arg-type]


def test_iso_is_sortable_and_utc() -> None:
    naive = datetime(2026, 11, 12, 14, 0)
    assert iso(naive).endswith("+00:00")
    amsterdam = parse_ts("2026-11-12T15:00:00+01:00")
    assert amsterdam is not None
    assert iso(amsterdam) == iso(TS)
    assert iso(TS) < iso(datetime(2026, 11, 12, 14, 0, 1, tzinfo=timezone.utc))


def test_parse_ts_handles_z_and_garbage() -> None:
    assert parse_ts("2026-11-12T14:00:00Z") == TS
    assert parse_ts("geen datum") is None
    assert parse_ts("") is None
    assert parse_ts(None) is None


def test_readings_are_idempotent(tmp_path) -> None:
    with Store(tmp_path / "db.sqlite") as store:
        store.upsert_entity(EntityMeta("sensor.cop", "COP"))
        first = store.add_readings([Reading.from_state("sensor.cop", "4.37", TS)])
        again = store.add_readings([Reading.from_state("sensor.cop", "4.37", TS)])
        assert (first, again) == (1, 0)
        assert store.stats()["readings"] == 1


def test_entity_metadata_is_merged_not_overwritten(tmp_path) -> None:
    with Store(tmp_path / "db.sqlite") as store:
        store.upsert_entity(
            EntityMeta("sensor.p", "Power", unit="W", device_class="power", platform="weheat")
        )
        # Een tweede ronde zonder eenheid mag de bekende eenheid niet wissen.
        store.upsert_entity(EntityMeta("sensor.p", "Power", unit=None))
        row = store.entities()[0]
        assert row["unit"] == "W"
        assert row["device_class"] == "power"
        assert row["platform"] == "weheat"


def test_attributes_only_stored_on_change(tmp_path) -> None:
    with Store(tmp_path / "db.sqlite") as store:
        attrs = {"options": ["idle", "heating"], "device_class": "enum"}
        assert store.add_attributes("sensor.mode", TS, attrs) is True
        assert store.add_attributes("sensor.mode", TS.replace(second=30), attrs) is False
        changed = {**attrs, "device_class": "other"}
        assert store.add_attributes("sensor.mode", TS.replace(second=45), changed) is True
        assert store.stats()["attributes"] == 2


def test_statistics_accept_epoch_millis_and_replace(tmp_path) -> None:
    with Store(tmp_path / "db.sqlite") as store:
        epoch_ms = TS.timestamp() * 1000
        assert store.add_statistics("sensor.cop", "hour", [{"start": epoch_ms, "mean": 4.0}]) == 1
        # Dezelfde periode nogmaals: bijwerken, geen extra rij.
        assert store.add_statistics("sensor.cop", "hour", [{"start": epoch_ms, "mean": 4.5}]) == 0
        row = store.conn.execute("SELECT mean FROM statistics").fetchone()
        assert row["mean"] == 4.5


def test_gaps_open_once_and_close(tmp_path) -> None:
    with Store(tmp_path / "db.sqlite") as store:
        first = store.open_gap("verbinding weg")
        second = store.open_gap("nog een keer")
        assert first == second, "een tweede gat mag niet naast het eerste openen"
        assert store.stats()["open_gaps"] == 1
        assert store.close_gaps() == 1
        assert store.stats()["open_gaps"] == 0


def test_view_joins_metadata(tmp_path) -> None:
    with Store(tmp_path / "db.sqlite") as store:
        store.upsert_entity(EntityMeta("sensor.t", "Buitentemperatuur", unit="°C"))
        store.add_readings([Reading.from_state("sensor.t", "13.2", TS)])
        row = store.conn.execute("SELECT * FROM v_readings").fetchone()
        assert row["friendly_name"] == "Buitentemperatuur"
        assert row["unit"] == "°C"
        assert row["value_num"] == 13.2
