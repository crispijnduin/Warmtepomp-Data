"""Duurzame opslag van warmtepompdata in SQLite.

Waarom SQLite: de database is één bestand, is zonder server te back-uppen en
blijft met SQL te bevragen zonder dat er iets geïnstalleerd hoeft te worden.
Alle schrijfacties zijn idempotent (INSERT OR IGNORE op de primaire sleutel),
zodat een backfill of herstart nooit dubbele rijen oplevert.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .util import iso, to_float, utcnow

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Eén rij per entiteit met de laatst bekende metadata.
CREATE TABLE IF NOT EXISTS entities (
    entity_id     TEXT PRIMARY KEY,
    friendly_name TEXT,
    unit          TEXT,
    device_class  TEXT,
    state_class   TEXT,
    domain        TEXT,
    device_id     TEXT,
    device_name   TEXT,
    platform      TEXT,
    options       TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);

-- Meetwaarden op volle resolutie. Eén rij per entiteit per tijdstip.
CREATE TABLE IF NOT EXISTS readings (
    entity_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    value_num  REAL,
    value_text TEXT,
    source     TEXT NOT NULL,
    PRIMARY KEY (entity_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);

-- Attributen, alleen weggeschreven als ze veranderen.
CREATE TABLE IF NOT EXISTS attributes (
    entity_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,
    attrs_json TEXT NOT NULL,
    PRIMARY KEY (entity_id, ts)
) WITHOUT ROWID;

-- Langetermijnstatistieken uit de Home Assistant recorder.
CREATE TABLE IF NOT EXISTS statistics (
    entity_id TEXT NOT NULL,
    period    TEXT NOT NULL,
    start     TEXT NOT NULL,
    mean      REAL,
    min       REAL,
    max       REAL,
    state     REAL,
    sum       REAL,
    change    REAL,
    last_reset TEXT,
    PRIMARY KEY (entity_id, period, start)
) WITHOUT ROWID;

-- Logboek van capture-sessies, zodat achteraf te zien is wat er liep.
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    mode       TEXT NOT NULL,
    events     INTEGER NOT NULL DEFAULT 0,
    reconnects INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);

-- Perioden zonder verbinding: hier mist bewust data.
CREATE TABLE IF NOT EXISTS gaps (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    reason     TEXT
);

CREATE INDEX IF NOT EXISTS idx_gaps_open ON gaps (ended_at);

-- Analyseklaar overzicht: meetwaarde met eenheid en naam erbij.
CREATE VIEW IF NOT EXISTS v_readings AS
SELECT r.ts,
       r.entity_id,
       e.friendly_name,
       r.value_num,
       r.value_text,
       e.unit,
       e.device_class,
       r.source
FROM readings AS r
LEFT JOIN entities AS e USING (entity_id);
"""


@dataclass(frozen=True)
class Reading:
    """Eén meetwaarde van één entiteit."""

    entity_id: str
    ts: datetime
    value_text: str | None
    value_num: float | None = None
    source: str = "event"

    @classmethod
    def from_state(
        cls, entity_id: str, state: str | None, ts: datetime, source: str = "event"
    ) -> Reading:
        """Maak een Reading en leid de numerieke waarde af uit de state."""
        return cls(
            entity_id=entity_id,
            ts=ts,
            value_text=state,
            value_num=to_float(state),
            source=source,
        )


@dataclass(frozen=True)
class EntityMeta:
    """Metadata van een entiteit zoals Home Assistant die kent."""

    entity_id: str
    friendly_name: str | None = None
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    device_id: str | None = None
    device_name: str | None = None
    platform: str | None = None
    options: Sequence[str] | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


class Store:
    """SQLite-opslag voor meetwaarden, statistieken en metadata."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        # WAL houdt lezen (analyse, export) en schrijven (capture) uit elkaars weg.
        if str(self.path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    # ---------------------------------------------------------------- lifecycle

    def close(self) -> None:
        # PRAGMA optimize is een nettigheid, geen voorwaarde om te sluiten.
        with contextlib.suppress(sqlite3.Error):
            self.conn.execute("PRAGMA optimize")
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- entities

    def upsert_entity(self, meta: EntityMeta) -> None:
        """Sla entiteitmetadata op; bestaande velden worden bijgewerkt.

        first_seen blijft staan, last_seen schuift mee. Een veld dat nu
        onbekend is overschrijft een eerder bekende waarde niet.
        """
        now = iso(utcnow())
        options = json.dumps(list(meta.options), ensure_ascii=False) if meta.options else None
        self.conn.execute(
            """
            INSERT INTO entities (entity_id, friendly_name, unit, device_class,
                                  state_class, domain, device_id, device_name,
                                  platform, options, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (entity_id) DO UPDATE SET
                friendly_name = COALESCE(excluded.friendly_name, friendly_name),
                unit          = COALESCE(excluded.unit, unit),
                device_class  = COALESCE(excluded.device_class, device_class),
                state_class   = COALESCE(excluded.state_class, state_class),
                device_id     = COALESCE(excluded.device_id, device_id),
                device_name   = COALESCE(excluded.device_name, device_name),
                platform      = COALESCE(excluded.platform, platform),
                options       = COALESCE(excluded.options, options),
                last_seen     = excluded.last_seen
            """,
            (
                meta.entity_id,
                meta.friendly_name,
                meta.unit,
                meta.device_class,
                meta.state_class,
                meta.domain,
                meta.device_id,
                meta.device_name,
                meta.platform,
                options,
                now,
                now,
            ),
        )

    def entity_ids(self) -> list[str]:
        rows = self.conn.execute("SELECT entity_id FROM entities ORDER BY entity_id")
        return [r["entity_id"] for r in rows]

    def entities(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM entities ORDER BY entity_id"))

    # ---------------------------------------------------------------- readings

    def add_reading(self, reading: Reading) -> int:
        """Schrijf één meetwaarde weg. Geeft 1 bij nieuw, 0 bij duplicaat."""
        return self.add_readings([reading])

    def add_readings(self, readings: Iterable[Reading]) -> int:
        """Schrijf meetwaarden weg in één transactie. Geeft het aantal nieuwe rijen."""
        rows = [
            (r.entity_id, iso(r.ts), r.value_num, r.value_text, r.source) for r in readings
        ]
        if not rows:
            return 0
        cur = self.conn.cursor()
        cur.execute("BEGIN")
        try:
            before = self._count("readings")
            cur.executemany(
                """INSERT OR IGNORE INTO readings
                   (entity_id, ts, value_num, value_text, source)
                   VALUES (?, ?, ?, ?, ?)""",
                rows,
            )
            after = self._count("readings")
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return after - before

    def add_attributes(self, entity_id: str, ts: datetime, attrs: dict[str, Any]) -> bool:
        """Sla attributen op als ze afwijken van de laatst bewaarde set.

        Geeft True als er daadwerkelijk iets is weggeschreven.
        """
        if not attrs:
            return False
        payload = json.dumps(attrs, ensure_ascii=False, sort_keys=True, default=str)
        row = self.conn.execute(
            "SELECT attrs_json FROM attributes WHERE entity_id = ? ORDER BY ts DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        if row is not None and row["attrs_json"] == payload:
            return False
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO attributes (entity_id, ts, attrs_json) VALUES (?, ?, ?)",
            (entity_id, iso(ts), payload),
        )
        return cur.rowcount > 0

    def last_reading_ts(self, entity_id: str) -> datetime | None:
        """Tijdstip van de laatste meetwaarde van een entiteit."""
        from .util import parse_ts

        row = self.conn.execute(
            "SELECT MAX(ts) AS ts FROM readings WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        return parse_ts(row["ts"]) if row and row["ts"] else None

    # -------------------------------------------------------------- statistics

    def add_statistics(self, entity_id: str, period: str, rows: Iterable[dict[str, Any]]) -> int:
        """Sla langetermijnstatistieken op. Geeft het aantal nieuwe rijen."""
        from .util import parse_ts

        prepared: list[tuple[Any, ...]] = []
        for row in rows:
            # parse_ts leest zowel ISO-8601 als epoch (de statistics-API
            # levert 'start' in milliseconden).
            start = parse_ts(row.get("start"))
            if start is None:
                continue
            last_reset = parse_ts(row.get("last_reset"))
            prepared.append(
                (
                    entity_id,
                    period,
                    iso(start),
                    _num(row.get("mean")),
                    _num(row.get("min")),
                    _num(row.get("max")),
                    _num(row.get("state")),
                    _num(row.get("sum")),
                    _num(row.get("change")),
                    iso(last_reset) if last_reset else None,
                )
            )
        if not prepared:
            return 0
        cur = self.conn.cursor()
        cur.execute("BEGIN")
        try:
            before = self._count("statistics")
            cur.executemany(
                """INSERT OR REPLACE INTO statistics
                   (entity_id, period, start, mean, min, max, state, sum, change, last_reset)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                prepared,
            )
            after = self._count("statistics")
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return after - before

    # -------------------------------------------------------------- runs/gaps

    def start_run(self, mode: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (started_at, mode) VALUES (?, ?)", (iso(utcnow()), mode)
        )
        return int(cur.lastrowid or 0)

    def finish_run(
        self, run_id: int, *, events: int = 0, reconnects: int = 0, error: str | None = None
    ) -> None:
        self.conn.execute(
            """UPDATE runs SET ended_at = ?, events = ?, reconnects = ?, last_error = ?
               WHERE id = ?""",
            (iso(utcnow()), events, reconnects, error, run_id),
        )

    def open_gap(self, reason: str, started_at: datetime | None = None) -> int:
        """Markeer het begin van een periode zonder data."""
        row = self.conn.execute(
            "SELECT id FROM gaps WHERE ended_at IS NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is not None:
            return int(row["id"])  # er loopt al een gat; niet nog een openen
        cur = self.conn.execute(
            "INSERT INTO gaps (started_at, reason) VALUES (?, ?)",
            (iso(started_at or utcnow()), reason),
        )
        return int(cur.lastrowid or 0)

    def close_gaps(self, ended_at: datetime | None = None) -> int:
        cur = self.conn.execute(
            "UPDATE gaps SET ended_at = ? WHERE ended_at IS NULL",
            (iso(ended_at or utcnow()),),
        )
        return cur.rowcount

    # ------------------------------------------------------------------ status

    def stats(self) -> dict[str, Any]:
        """Samenvatting voor 'warmtepomp status'."""
        row = self.conn.execute(
            """SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts
               FROM readings"""
        ).fetchone()
        return {
            "db_path": str(self.path),
            "db_bytes": self.path.stat().st_size if self.path.is_file() else 0,
            "entities": self._count("entities"),
            "readings": int(row["n"] or 0),
            "first_reading": row["first_ts"],
            "last_reading": row["last_ts"],
            "attributes": self._count("attributes"),
            "statistics": self._count("statistics"),
            "runs": self._count("runs"),
            "open_gaps": int(
                self.conn.execute(
                    "SELECT COUNT(*) AS n FROM gaps WHERE ended_at IS NULL"
                ).fetchone()["n"]
            ),
        }

    def per_entity_stats(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT e.entity_id,
                   e.friendly_name,
                   e.unit,
                   COUNT(r.ts)   AS readings,
                   MIN(r.ts)     AS first_ts,
                   MAX(r.ts)     AS last_ts
            FROM entities AS e
            LEFT JOIN readings AS r USING (entity_id)
            GROUP BY e.entity_id
            ORDER BY e.entity_id
            """
        )
        return [dict(r) for r in rows]

    def _count(self, table: str) -> int:
        row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()  # noqa: S608
        return int(row["n"])


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
