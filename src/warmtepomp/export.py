"""Exporteren naar bestanden die elk analysetool kan lezen.

Drie vormen:

* `readings`  - lange tabel met alle meetwaarden (één rij per meting);
* `wide`      - brede tabel, één kolom per entiteit, gelijkgezet op een raster;
* `statistics`- de langetermijnstatistieken per periode.

CSV werkt altijd; Parquet alleen als `pyarrow` is geïnstalleerd.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .store import Store
from .util import iso, parse_ts

_LOG = logging.getLogger(__name__)

RESAMPLE_SECONDS = {
    "raw": 0,
    "1min": 60,
    "5min": 300,
    "15min": 900,
    "hour": 3600,
    "day": 86400,
}


@dataclass
class ExportResult:
    """Overzicht van één export."""

    files: list[Path]
    rows: int

    def as_dict(self) -> dict[str, Any]:
        return {"files": [str(f) for f in self.files], "rows": self.rows}


def _write_csv(path: Path, header: Sequence[str], rows: Iterator[Sequence[Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def _maybe_parquet(path: Path, header: Sequence[str], rows: list[Sequence[Any]]) -> Path | None:
    """Schrijf Parquet als pyarrow beschikbaar is, anders None."""
    try:
        import pyarrow as pa  # noqa: PLC0415
        import pyarrow.parquet as pq  # noqa: PLC0415
    except ImportError:
        return None
    columns = {name: [row[i] for row in rows] for i, name in enumerate(header)}
    table = pa.table(columns)
    target = path.with_suffix(".parquet")
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, target, compression="zstd")
    return target


def export_readings(
    store: Store,
    out_dir: Path,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    entity_ids: Sequence[str] | None = None,
    parquet: bool = False,
    name: str = "readings",
) -> ExportResult:
    """Exporteer alle meetwaarden als lange tabel."""
    where: list[str] = []
    params: list[Any] = []
    if start is not None:
        where.append("r.ts >= ?")
        params.append(iso(start))
    if end is not None:
        where.append("r.ts < ?")
        params.append(iso(end))
    if entity_ids:
        where.append(f"r.entity_id IN ({','.join('?' * len(entity_ids))})")
        params.extend(entity_ids)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT r.ts, r.entity_id, e.friendly_name, r.value_num, r.value_text,
               e.unit, e.device_class, r.source
        FROM readings AS r
        LEFT JOIN entities AS e USING (entity_id)
        {clause}
        ORDER BY r.ts, r.entity_id
    """  # noqa: S608 - placeholders worden gebonden, alleen hun aantal is dynamisch
    header = [
        "ts",
        "entity_id",
        "friendly_name",
        "value_num",
        "value_text",
        "unit",
        "device_class",
        "source",
    ]
    cursor = store.conn.execute(sql, params)
    rows = [tuple(r) for r in cursor]
    csv_path = out_dir / f"{name}.csv"
    count = _write_csv(csv_path, header, iter(rows))
    files = [csv_path]
    if parquet:
        produced = _maybe_parquet(csv_path, header, rows)
        if produced is not None:
            files.append(produced)
        else:
            _LOG.warning("pyarrow ontbreekt; Parquet overgeslagen (pip install pyarrow)")
    return ExportResult(files=files, rows=count)


def export_wide(
    store: Store,
    out_dir: Path,
    *,
    resample: str = "5min",
    start: datetime | None = None,
    end: datetime | None = None,
    parquet: bool = False,
    name: str | None = None,
) -> ExportResult:
    """Exporteer een brede tabel: één rij per tijdstap, één kolom per entiteit.

    Per tijdstap wordt de laatst bekende numerieke waarde genomen, zodat de
    tabel ook bruikbaar is voor entiteiten die zelden wijzigen.
    """
    if resample not in RESAMPLE_SECONDS:
        raise ValueError(
            f"onbekende resample {resample!r}; kies uit {', '.join(RESAMPLE_SECONDS)}"
        )
    step = RESAMPLE_SECONDS[resample]
    entity_ids = [
        row["entity_id"]
        for row in store.conn.execute(
            """SELECT DISTINCT entity_id FROM readings
               WHERE value_num IS NOT NULL ORDER BY entity_id"""
        )
    ]
    if not entity_ids:
        return ExportResult(files=[], rows=0)

    where: list[str] = ["value_num IS NOT NULL"]
    params: list[Any] = []
    if start is not None:
        where.append("ts >= ?")
        params.append(iso(start))
    if end is not None:
        where.append("ts < ?")
        params.append(iso(end))
    sql = f"""SELECT ts, entity_id, value_num FROM readings
              WHERE {' AND '.join(where)} ORDER BY ts"""  # noqa: S608
    index = {entity_id: i for i, entity_id in enumerate(entity_ids)}
    current: list[float | None] = [None] * len(entity_ids)
    rows: list[tuple[Any, ...]] = []
    bucket_key: int | None = None

    def bucket_of(ts: datetime) -> int:
        epoch = int(ts.timestamp())
        return epoch if step == 0 else epoch - (epoch % step)

    for row in store.conn.execute(sql, params):
        ts = parse_ts(row["ts"])
        if ts is None:
            continue
        key = bucket_of(ts)
        if bucket_key is None:
            bucket_key = key
        elif key != bucket_key:
            rows.append(_wide_row(bucket_key, current))
            bucket_key = key
        current[index[row["entity_id"]]] = row["value_num"]
    if bucket_key is not None:
        rows.append(_wide_row(bucket_key, current))

    header = ["ts", *entity_ids]
    csv_path = out_dir / (name or f"wide_{resample}.csv")
    count = _write_csv(csv_path, header, iter(rows))
    files = [csv_path]
    if parquet:
        produced = _maybe_parquet(csv_path, header, rows)
        if produced is not None:
            files.append(produced)
    return ExportResult(files=files, rows=count)


def _wide_row(bucket: int, current: Sequence[float | None]) -> tuple[Any, ...]:
    from datetime import timezone

    ts = datetime.fromtimestamp(bucket, tz=timezone.utc)
    return (ts.isoformat(), *current)


def export_statistics(
    store: Store,
    out_dir: Path,
    *,
    period: str | None = None,
    parquet: bool = False,
) -> ExportResult:
    """Exporteer de langetermijnstatistieken."""
    sql = """SELECT s.period, s.start, s.entity_id, e.friendly_name, e.unit,
                    s.mean, s.min, s.max, s.state, s.sum, s.change
             FROM statistics AS s
             LEFT JOIN entities AS e USING (entity_id)"""
    params: list[Any] = []
    if period:
        sql += " WHERE s.period = ?"
        params.append(period)
    sql += " ORDER BY s.period, s.start, s.entity_id"
    header = [
        "period",
        "start",
        "entity_id",
        "friendly_name",
        "unit",
        "mean",
        "min",
        "max",
        "state",
        "sum",
        "change",
    ]
    rows = [tuple(r) for r in store.conn.execute(sql, params)]
    csv_path = out_dir / (f"statistics_{period}.csv" if period else "statistics.csv")
    count = _write_csv(csv_path, header, iter(rows))
    files = [csv_path]
    if parquet:
        produced = _maybe_parquet(csv_path, header, rows)
        if produced is not None:
            files.append(produced)
    return ExportResult(files=files, rows=count)


def export_all(
    store: Store,
    out_dir: Path,
    *,
    resample: str = "5min",
    parquet: bool = False,
) -> dict[str, ExportResult]:
    """Exporteer alle drie de vormen in één keer."""
    out_dir.mkdir(parents=True, exist_ok=True)
    return {
        "readings": export_readings(store, out_dir, parquet=parquet),
        "wide": export_wide(store, out_dir, resample=resample, parquet=parquet),
        "statistics": export_statistics(store, out_dir, parquet=parquet),
    }


def entity_metadata_csv(store: Store, out_dir: Path) -> ExportResult:
    """Schrijf de entiteitenlijst weg; handig als dataset-documentatie."""
    rows: list[tuple[Any, ...]] = []
    header = [
        "entity_id",
        "friendly_name",
        "unit",
        "device_class",
        "state_class",
        "device_name",
        "platform",
        "readings",
        "first_ts",
        "last_ts",
    ]
    per_entity = {e["entity_id"]: e for e in store.per_entity_stats()}
    for row in store.entities():
        extra = per_entity.get(row["entity_id"], {})
        rows.append(
            (
                row["entity_id"],
                row["friendly_name"],
                row["unit"],
                row["device_class"],
                row["state_class"],
                row["device_name"],
                row["platform"],
                extra.get("readings", 0),
                extra.get("first_ts"),
                extra.get("last_ts"),
            )
        )
    path = out_dir / "entities.csv"
    count = _write_csv(path, header, iter(rows))
    return ExportResult(files=[path], rows=count)
