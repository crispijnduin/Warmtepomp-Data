"""Brug tussen de eigen database en Home Assistant.

De database staat op schijf in de app-container. Wie alleen via de Home
Assistant-API met dit systeem praat — een assistent, een dashboard, een
automatisering — kan daar niet bij. Deze module maakt de inhoud alsnog
bereikbaar, langs twee wegen die geen extra software vragen.

**Omhoog: samenvatting als entiteiten.** Wat Home Assistant zelf niet bewaart
wordt periodiek uitgerekend en als entiteit teruggezet. Een entiteit met een
state_class wordt door de recorder als langetermijnstatistiek bewaard, dus die
cijfers blijven staan nadat de losse metingen zijn opgeruimd. Dit gaat over
pendelen (hoe vaak slaat de compressor aan) en over de tijdsverdeling tussen
verwarmen, tapwater, ontdooien en stilstand — precies de twee dingen die in
een uurgemiddelde onzichtbaar zijn.

**Omlaag: vragen via het logboek.** `logbook.log` vuurt een `logbook_entry`
op de eventbus. De app luistert daarop en voert een leesvraag uit tegen de
database; het antwoord komt terug als entiteit. Zo is elke SQL-vraag te
stellen met alleen de gewone Home Assistant-API, zonder poorten, wachtwoorden
of handmatig aangemaakte helpers.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from .store import Store
from .util import iso, utcnow

_LOG = logging.getLogger(__name__)

# Voorvoegsel van de entiteiten die deze module aanmaakt.
PREFIX = "sensor.warmtepomp_"
QUERY_RESULT_ENTITY = f"{PREFIX}query_result"

# Naam waarop een logboekregel als vraag wordt herkend.
DEFAULT_QUERY_MARKER = "warmtepomp-query"

# Boven dit opgenomen vermogen (W) draait de compressor echt.
RUNNING_WATTS = 100.0

# Een antwoord moet in een entiteit passen; Home Assistant kapt een state af
# op 255 tekens, dus het echte antwoord gaat in de attributen.
MAX_RESULT_ROWS = 200
MAX_RESULT_CHARS = 14000

# 'replace' staat hier niet als los woord. REPLACE() is in SQLite ook een
# gewone tekstfunctie, en die weigeren maakte onschuldige leesvragen
# onmogelijk: SELECT replace(entity_id, 'sensor.', '') kwam niet door de
# controle. Geweerd wordt alleen de schrijfvorm REPLACE INTO; INSERT OR
# REPLACE ketst al op 'insert' af.
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum)\b"
    r"|\breplace\s+into\b",
    re.IGNORECASE,
)


class QueryRefused(ValueError):
    """De vraag is geweigerd omdat hij niet alleen leest."""


# --------------------------------------------------------------------- omhoog


@dataclass
class DigestEntity:
    """Eén entiteit die naar Home Assistant wordt gepubliceerd."""

    entity_id: str
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


def _find(store: Store, suffix: str) -> str | None:
    row = store.conn.execute(
        "SELECT entity_id FROM entities WHERE entity_id LIKE ? ORDER BY entity_id LIMIT 1",
        (f"%{suffix}",),
    ).fetchone()
    return row["entity_id"] if row else None


def _state_before(store: Store, entity_id: str, before: str) -> Any:
    """De laatst bekende meting vóór een tijdstip.

    Nodig om te weten in welke toestand de pomp het tijdvenster in ging. Zonder
    dit lijkt een pomp die al een uur stil op 'heating' stond helemaal geen
    modus te hebben, en telt een compressor die al draaide als nieuwe start.
    """
    return store.conn.execute(
        """SELECT ts, value_num, value_text FROM readings
           WHERE entity_id = ? AND ts < ?
           ORDER BY ts DESC LIMIT 1""",
        (entity_id, before),
    ).fetchone()


def count_compressor_starts(store: Store, hours: float = 1.0) -> int | None:
    """Tel hoe vaak de compressor in de afgelopen periode is aangeslagen.

    Pendelen — vaak starten en stoppen in plaats van rustig moduleren — is de
    meest voorkomende inregelfout en kost rendement. Binnen een uurgemiddelde
    is het onzichtbaar: twintig korte pieken en één constante belasting geven
    hetzelfde gemiddelde.

    Een pomp die bij het begin van het venster al draaide telt niet als start;
    alleen de overgang van stil naar draaiend telt.
    """
    entity_id = _find(store, "_input_power")
    if not entity_id:
        return None
    since = iso(utcnow() - timedelta(hours=hours))
    rows = store.conn.execute(
        """SELECT value_num FROM readings
           WHERE entity_id = ? AND ts >= ? AND value_num IS NOT NULL
           ORDER BY ts""",
        (entity_id, since),
    ).fetchall()
    previous = _state_before(store, entity_id, since)
    if not rows and previous is None:
        return None

    # Begin met de toestand waarin het venster werd ingegaan.
    running = bool(
        previous is not None
        and previous["value_num"] is not None
        and float(previous["value_num"]) >= RUNNING_WATTS
    )
    starts = 0
    for row in rows:
        now_running = float(row["value_num"]) >= RUNNING_WATTS
        if now_running and not running:
            starts += 1
        running = now_running
    return starts


def _mode_entity(store: Store) -> str | None:
    """Zoek de entiteit die de bedrijfsmodus bijhoudt.

    Niet op naam, want die verschilt per merk en model. Gezocht wordt naar een
    entiteit die toestanden als 'heating' of 'defrosting' heeft gemeld; is die
    er niet, dan de kortst genoemde enum-entiteit, want dat is doorgaans de
    hoofdsensor van het apparaat.
    """
    known = ("heating", "cooling", "dhw", "defrosting", "standby", "legionella_prevention")
    placeholders = ",".join("?" * len(known))
    row = store.conn.execute(
        f"""SELECT r.entity_id, COUNT(*) AS hits
            FROM readings AS r
            WHERE r.value_text IN ({placeholders})
            GROUP BY r.entity_id
            ORDER BY hits DESC, length(r.entity_id)
            LIMIT 1""",  # noqa: S608 - alleen het aantal placeholders is dynamisch
        known,
    ).fetchone()
    if row:
        return str(row["entity_id"])
    row = store.conn.execute(
        """SELECT entity_id FROM entities
           WHERE device_class = 'enum'
           ORDER BY length(entity_id) LIMIT 1"""
    ).fetchone()
    return str(row["entity_id"]) if row else None


# Hoe ver wordt teruggekeken om te weten welke modi deze installatie kent.
KNOWN_MODE_DAYS = 30
MAX_MODE_ENTITIES = 20


def known_modes(store: Store, days: int = KNOWN_MODE_DAYS) -> list[str]:
    """Welke bedrijfsmodi deze installatie de afgelopen tijd heeft gemeld.

    Nodig om een modus die uit het tijdvenster loopt op nul te kunnen zetten.
    Zonder dat blijft zijn sensor op de laatst gemeten waarde staan en lijkt
    de pomp nog steeds te ontdooien terwijl dat al een uur voorbij is.
    """
    entity_id = _mode_entity(store)
    if not entity_id:
        return []
    since = iso(utcnow() - timedelta(days=days))
    rows = store.conn.execute(
        """SELECT value_text, COUNT(*) AS hits
           FROM readings
           WHERE entity_id = ? AND ts >= ? AND value_text IS NOT NULL
           GROUP BY value_text
           ORDER BY hits DESC
           LIMIT ?""",
        (entity_id, since, MAX_MODE_ENTITIES),
    ).fetchall()
    return sorted(str(row["value_text"]) for row in rows)


def minutes_per_mode(store: Store, hours: float = 1.0) -> dict[str, float]:
    """Hoeveel minuten de pomp in elke bedrijfsmodus stond.

    De bedrijfsmodus is tekst, dus Home Assistant bewaart hem niet als
    statistiek. Elke meting telt tot aan de volgende; de laatste telt tot nu.
    De toestand waarin het venster werd ingegaan telt mee vanaf het begin van
    het venster, zodat een lange ongewijzigde periode niet verdwijnt.
    """
    from .util import parse_ts

    entity_id = _mode_entity(store)
    if not entity_id:
        return {}
    now = utcnow()
    since = now - timedelta(hours=hours)
    rows = store.conn.execute(
        """SELECT ts, value_text FROM readings
           WHERE entity_id = ? AND ts >= ? AND value_text IS NOT NULL
           ORDER BY ts""",
        (entity_id, iso(since)),
    ).fetchall()

    # Segmenten als (starttijd, modus), beginnend bij het begin van het venster.
    segments: list[tuple[Any, str]] = []
    previous = _state_before(store, entity_id, iso(since))
    if previous is not None and previous["value_text"]:
        segments.append((since, str(previous["value_text"])))
    for row in rows:
        start = parse_ts(row["ts"])
        if start is None:
            continue
        segments.append((max(start, since), str(row["value_text"])))
    if not segments:
        return {}

    totals: dict[str, float] = {}
    for index, (start, mode) in enumerate(segments):
        end = segments[index + 1][0] if index + 1 < len(segments) else now
        seconds = max((end - start).total_seconds(), 0.0)
        if seconds <= 0:
            continue
        totals[mode] = totals.get(mode, 0.0) + seconds / 60.0
    return {mode: round(value, 2) for mode, value in sorted(totals.items())}


def capture_health(store: Store) -> dict[str, Any]:
    """Hoe de capture ervoor staat: aantal metingen en hoe vers ze zijn."""
    from .util import parse_ts

    stats = store.stats()
    last = parse_ts(stats["last_reading"]) if stats["last_reading"] else None
    lag = (utcnow() - last).total_seconds() if last else None
    return {
        "readings": stats["readings"],
        "entities": stats["entities"],
        "statistics": stats["statistics"],
        "first_reading": stats["first_reading"],
        "last_reading": stats["last_reading"],
        "lag_seconds": round(lag, 1) if lag is not None else None,
        "open_gaps": stats["open_gaps"],
        "db_megabytes": round(stats["db_bytes"] / 1_048_576, 2),
    }


def build_digest(store: Store) -> list[DigestEntity]:
    """Stel de entiteiten samen die naar Home Assistant gaan.

    Alleen afgeleiden die Home Assistant zelf niet kan bewaren. Alles met een
    state_class wordt door de recorder als uurstatistiek bewaard en blijft dus
    ook over een jaar nog opvraagbaar.
    """
    entities: list[DigestEntity] = []

    starts = count_compressor_starts(store, hours=1.0)
    if starts is not None:
        entities.append(
            DigestEntity(
                entity_id=f"{PREFIX}compressor_starts_1h",
                state=str(starts),
                attributes={
                    "friendly_name": "Warmtepomp compressorstarts per uur",
                    "state_class": "measurement",
                    "unit_of_measurement": "starts/h",
                    "icon": "mdi:restart",
                },
            )
        )

    modes = minutes_per_mode(store, hours=1.0)
    if modes:
        # Eén entiteit per modus, zodat elk een eigen statistiekreeks krijgt.
        # Ook modi die dit uur niet voorkwamen worden gepubliceerd, met nul,
        # anders blijft hun sensor op de waarde van een vorig uur staan.
        for mode in sorted(set(known_modes(store)) | set(modes)):
            minutes = modes.get(mode, 0.0)
            safe = re.sub(r"[^a-z0-9_]+", "_", mode.lower()).strip("_") or "onbekend"
            entities.append(
                DigestEntity(
                    entity_id=f"{PREFIX}minutes_{safe}_1h",
                    state=f"{minutes:.2f}",
                    attributes={
                        "friendly_name": f"Warmtepomp minuten {mode} per uur",
                        "state_class": "measurement",
                        "unit_of_measurement": "min",
                        "icon": "mdi:timer-outline",
                    },
                )
            )
        dominant = max(modes.items(), key=lambda item: item[1])[0]
        entities.append(
            DigestEntity(
                entity_id=f"{PREFIX}mode_last_hour",
                state=dominant[:255],
                attributes={
                    "friendly_name": "Warmtepomp hoofdmodus afgelopen uur",
                    "minutes_per_mode": modes,
                    "icon": "mdi:heat-pump-outline",
                },
            )
        )

    health = capture_health(store)
    entities.append(
        DigestEntity(
            entity_id=f"{PREFIX}capture_readings",
            state=str(health["readings"]),
            attributes={
                "friendly_name": "Warmtepomp vastgelegde metingen",
                "state_class": "total_increasing",
                "unit_of_measurement": "metingen",
                "icon": "mdi:database",
                **{k: v for k, v in health.items() if k != "readings"},
            },
        )
    )
    return entities


async def publish_digest(client: Any, store: Store) -> int:
    """Reken de samenvatting uit en zet hem in Home Assistant."""
    published = 0
    for entity in build_digest(store):
        try:
            await client.set_state(entity.entity_id, entity.state, entity.attributes)
            published += 1
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Kon %s niet publiceren: %s", entity.entity_id, exc)
    return published


# --------------------------------------------------------------------- omlaag


def run_query(store: Store, sql: str, limit: int = MAX_RESULT_ROWS) -> dict[str, Any]:
    """Voer een leesvraag uit tegen de database.

    Alleen SELECT en WITH zijn toegestaan, en de database wordt bovendien in
    alleen-lezen modus bevraagd. Een vraag die toch iets wil wijzigen wordt
    geweigerd, niet stilzwijgend genegeerd.
    """
    statement = sql.strip().rstrip(";").strip()
    if not statement:
        raise QueryRefused("lege vraag")
    lowered = statement.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise QueryRefused("alleen SELECT- of WITH-vragen zijn toegestaan")
    if _FORBIDDEN.search(statement):
        raise QueryRefused("de vraag bevat een bewerking die niet mag lezen")
    if ";" in statement:
        raise QueryRefused("meerdere opdrachten in één vraag zijn niet toegestaan")

    cursor = store.conn.execute(statement)
    columns = [d[0] for d in cursor.description or []]
    rows = [list(row) for row in cursor.fetchmany(limit)]
    truncated = cursor.fetchone() is not None
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
    }


def _fit(result: dict[str, Any]) -> dict[str, Any]:
    """Kort het antwoord in tot het in de attributen van een entiteit past."""
    payload = dict(result)
    while len(json.dumps(payload, default=str)) > MAX_RESULT_CHARS and payload["rows"]:
        payload["rows"] = payload["rows"][: max(len(payload["rows"]) // 2, 1)]
        payload["row_count"] = len(payload["rows"])
        payload["truncated"] = True
        if len(payload["rows"]) == 1:
            break
    return payload


async def answer_query(client: Any, store: Store, sql: str) -> dict[str, Any]:
    """Beantwoord een vraag en zet het antwoord in Home Assistant."""
    asked_at = iso(utcnow())
    try:
        result = _fit(run_query(store, sql))
        state = f"{result['row_count']} rijen"
        attributes = {
            "friendly_name": "Warmtepomp antwoord op vraag",
            "query": sql[:1000],
            "asked_at": asked_at,
            "ok": True,
            "icon": "mdi:database-search",
            **result,
        }
        _LOG.info("Vraag beantwoord: %s rijen voor %r", result["row_count"], sql[:200])
    except Exception as exc:  # noqa: BLE001 - elke fout hoort in het antwoord
        state = "fout"
        attributes = {
            "friendly_name": "Warmtepomp antwoord op vraag",
            "query": sql[:1000],
            "asked_at": asked_at,
            "ok": False,
            "error": str(exc)[:500],
            "icon": "mdi:database-alert",
        }
        _LOG.warning("Vraag mislukt (%s): %r", exc, sql[:200])
    await client.set_state(QUERY_RESULT_ENTITY, state, attributes)
    return attributes


def query_from_logbook_event(event: dict[str, Any], marker: str) -> str | None:
    """Haal de SQL uit een logbook_entry-event, of None als het er geen is."""
    data = event.get("data") or {}
    name = str(data.get("name") or "").strip().lower()
    if name != marker.lower():
        return None
    message = str(data.get("message") or "").strip()
    return message or None
