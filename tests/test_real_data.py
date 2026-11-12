"""Regressietests met echte data van een Weheat Flint P40.

De cijfers in tests/data/ komen uit een werkende installatie. Ze staan hier
omdat juist de rommelige gevallen - negatieve COP, standby-nullen en een
buitentemperatuursensor die in de zon oploopt - laten zien of de analyse klopt.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from warmtepomp.analyse import (
    cop_vs_outside_temperature,
    daily_energy,
    data_quality,
    seasonal_cop,
)
from warmtepomp.store import EntityMeta, Reading, Store

DATA_DIR = Path(__file__).parent / "data"
PREFIX = "sensor.home_flint_p40_heat_pump"


def _rows(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open(encoding="utf-8") as handle:
        lines = [line for line in handle if not line.startswith("#")]
    return list(csv.DictReader(lines))


@pytest.fixture
def real_store(tmp_path: Path) -> Store:
    """Een database gevuld met de echte meetreeksen."""
    store = Store(tmp_path / "real.sqlite")
    for entity_id, name, unit in [
        (f"{PREFIX}_cop", "COP", None),
        (f"{PREFIX}_outside_temperature", "Outside temperature", "°C"),
        (f"{PREFIX}_electricity_used", "Electricity used", "kWh"),
        (f"{PREFIX}_total_energy_output", "Total energy output", "kWh"),
    ]:
        store.upsert_entity(EntityMeta(entity_id, name, unit=unit))

    for row in _rows("weheat_flint_p40_real.csv"):
        start = row["start"]
        store.add_statistics(
            f"{PREFIX}_cop",
            "hour",
            [{"start": start, "mean": row["cop_mean"], "min": row["cop_min"], "max": row["cop_max"]}],
        )
        store.add_statistics(
            f"{PREFIX}_outside_temperature",
            "hour",
            [{"start": start, "mean": row["outside_mean"], "max": row["outside_max"]}],
        )
        # De ruwe uitschieters ook als meetwaarde, voor de kwaliteitscontrole.
        store.add_readings(
            [
                Reading.from_state(f"{PREFIX}_cop", row["cop_min"], _ts(start)),
                Reading.from_state(
                    f"{PREFIX}_outside_temperature", row["outside_max"], _ts(start)
                ),
            ]
        )

    for row in _rows("weheat_flint_p40_daily.csv"):
        start = f"{row['day']}T00:00:00+01:00"
        store.add_statistics(
            f"{PREFIX}_electricity_used", "day", [{"start": start, "change": row["electricity_kwh"]}]
        )
        store.add_statistics(
            f"{PREFIX}_total_energy_output", "day", [{"start": start, "change": row["output_kwh"]}]
        )
    yield store
    store.close()


def _ts(value: str) -> datetime:
    from warmtepomp.util import parse_ts

    parsed = parse_ts(value)
    assert parsed is not None
    return parsed


def test_real_daily_energy_matches_the_meter(real_store: Store) -> None:
    rows = daily_energy(real_store, "Europe/Amsterdam")
    assert len(rows) == 6
    by_day = {r.day: r for r in rows}
    # 6 november: 11,41 kWh stroom voor 29,14 kWh warmte.
    first = by_day["2026-11-06"]
    assert first.electricity_kwh == pytest.approx(11.4125)
    assert first.output_kwh == pytest.approx(29.1358)
    assert first.cop == pytest.approx(2.553, abs=0.01)
    assert sorted(by_day) == [
        "2026-11-06",
        "2026-11-07",
        "2026-11-08",
        "2026-11-09",
        "2026-11-10",
        "2026-11-11",
    ]


def test_days_are_labelled_in_local_time(real_store: Store) -> None:
    """Een dag die om 00:00 Amsterdamse tijd begint hoort bij die dag.

    Home Assistant slaat de dagstatistiek op als 23:00 UTC van de dag ervoor.
    Zonder omrekening naar lokale tijd zou elke dag een dag te vroeg worden
    gelabeld en zou het dagverbruik niet kloppen met de energierekening.
    """
    amsterdam = {r.day for r in daily_energy(real_store, "Europe/Amsterdam")}
    utc = {r.day for r in daily_energy(real_store, "UTC")}
    assert "2026-11-06" in amsterdam
    assert "2026-11-05" in utc, "in UTC schuift dezelfde meting een dag terug"
    assert "2026-11-05" not in amsterdam


def test_real_scop_is_plausible_for_november(real_store: Store) -> None:
    total = seasonal_cop(real_store)
    assert total["electricity_kwh"] == pytest.approx(99.894, abs=0.01)
    assert total["output_kwh"] == pytest.approx(303.703, abs=0.01)
    # Een SCOP rond 3 is normaal voor een lucht-water-warmtepomp in november.
    assert total["scop"] == pytest.approx(3.04, abs=0.05)
    assert 2.0 < total["scop"] < 5.0


def test_cop_curve_ignores_standby_and_defrost_noise(real_store: Store) -> None:
    """Zonder uur-energie valt de curve terug op de sensor, maar netjes."""
    curve = cop_vs_outside_temperature(real_store, bucket=4.0)
    assert curve, "er zijn genoeg positieve uren voor een curve"
    # Geen enkele klasse mag een negatieve of absurde COP opleveren, ook al
    # staan er waarden van -12 en 0 in de ruwe data.
    for bucket in curve:
        assert bucket["cop_mean"] is not None
        assert 0 < bucket["cop_mean"] < 8, bucket


def test_energy_based_cop_curve_is_preferred(real_store: Store) -> None:
    """Met uur-energiestatistieken wordt de COP uit de tellers berekend."""
    base = datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)
    for hour in range(12):
        start = base + timedelta(hours=hour)
        real_store.add_statistics(
            f"{PREFIX}_outside_temperature", "hour", [{"start": start, "mean": 2.0 + hour}]
        )
        real_store.add_statistics(
            f"{PREFIX}_electricity_used", "hour", [{"start": start, "change": 1.0}]
        )
        real_store.add_statistics(
            f"{PREFIX}_total_energy_output",
            "hour",
            # Warmer buiten, hogere opbrengst: precies wat een warmtepomp doet.
            [{"start": start, "change": 2.5 + hour * 0.1}],
        )
    curve = cop_vs_outside_temperature(real_store, bucket=4.0)
    assert curve
    assert all(b["electricity_kwh"] > 0 for b in curve)
    # De COP hoort toe te nemen met de buitentemperatuur.
    cops = [b["cop_mean"] for b in curve if b["cop_mean"] is not None]
    assert cops == sorted(cops), f"COP hoort te stijgen met de temperatuur: {cops}"


def test_data_quality_flags_the_real_problems(real_store: Store) -> None:
    """De bekende meetfouten van deze installatie worden gesignaleerd."""
    report = data_quality(real_store)
    issues = {f["issue"]: f for f in report["findings"]}

    assert "negatieve COP" in issues, "ontdooicycli geven een negatieve COP"
    assert issues["negatieve COP"]["worst"] == pytest.approx(-12.2412)
    assert "ontdooien" in issues["negatieve COP"]["explanation"]

    # 30,3 graden in november komt van de zon op de buitenunit, niet van het weer.
    assert "ongeloofwaardige buitentemperatuur" not in issues, (
        "30 graden valt binnen het plausibele bereik; alleen echt onmogelijke "
        "waarden horen gemeld te worden"
    )


def test_data_quality_flags_impossible_temperature(real_store: Store) -> None:
    real_store.add_readings(
        [
            Reading.from_state(
                f"{PREFIX}_outside_temperature", "87.5", datetime(2026, 11, 12, tzinfo=timezone.utc)
            )
        ]
    )
    issues = {f["issue"] for f in data_quality(real_store)["findings"]}
    assert "ongeloofwaardige buitentemperatuur" in issues


def test_data_quality_flags_a_sensor_that_went_silent(real_store: Store) -> None:
    """De kamertemperatuursensor viel in het echt na een dag stil."""
    real_store.upsert_entity(EntityMeta(f"{PREFIX}_current_room_temperature", "Room temp"))
    real_store.add_readings(
        [
            Reading.from_state(
                f"{PREFIX}_current_room_temperature",
                "20.0",
                datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc),
            )
        ]
    )
    real_store.add_readings(
        [
            Reading.from_state(
                f"{PREFIX}_cop", "4.4", datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)
            )
        ]
    )
    findings = data_quality(real_store)["findings"]
    silent = [f for f in findings if f["issue"] == "levert geen data meer"]
    assert any(f["entity_id"].endswith("_current_room_temperature") for f in silent)


def test_sensor_fallback_drops_standby_dominated_hours(real_store: Store) -> None:
    """Uren die vooral standby waren mogen de curve niet vervuilen.

    In de echte data staan uurgemiddelden van 0,11 en 0,15: dat zijn uren waarin
    de pomp bijna stilstond. Zulke waarden zijn geen rendement.
    """
    curve = cop_vs_outside_temperature(real_store, bucket=4.0)
    assert curve
    assert all(b["method"] == "cop-sensor" for b in curve)
    assert all(b["cop_mean"] >= 1.5 for b in curve), (
        f"standby-uren niet uitgefilterd: {[b['cop_mean'] for b in curve]}"
    )


def test_statistics_only_entities_are_not_called_broken(real_store: Store) -> None:
    """Een entiteit waarvan alleen statistieken zijn ingelezen is geen storing."""
    silent = {
        f["entity_id"]
        for f in data_quality(real_store)["findings"]
        if f["issue"] == "levert geen data meer"
    }
    assert not any(e.endswith("_electricity_used") for e in silent)
    assert not any(e.endswith("_total_energy_output") for e in silent)
