"""Bestaande historie uit Home Assistant overnemen.

Home Assistant bewaart volle-resolutie states maar een beperkte tijd
(standaard tien dagen) en daarna alleen langetermijnstatistieken. Deze module
haalt beide binnen, zodat de eigen database ook de periode dekt van vóór het
moment dat de capture-service werd aangezet.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .discovery import discover
from .ha_client import HAClient
from .store import EntityMeta, Reading, Store
from .util import parse_ts, utcnow

_LOG = logging.getLogger(__name__)

# De statistics-API verdraagt niet onbeperkt veel id's per vraag.
_STATS_CHUNK = 25
_HISTORY_CHUNK = 10


@dataclass
class BackfillResult:
    """Wat een backfill heeft opgeleverd."""

    statistics_rows: int = 0
    reading_rows: int = 0
    entities: int = 0
    periods: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "statistics_rows": self.statistics_rows,
            "reading_rows": self.reading_rows,
            "entities": self.entities,
            "periods": dict(self.periods),
            "errors": list(self.errors),
        }


def _chunks(items: Sequence[str], size: int) -> list[list[str]]:
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


async def backfill(
    config: Any,
    client: HAClient,
    store: Store,
    *,
    days: int | None = None,
    periods: Sequence[str] | None = None,
    include_history: bool = True,
    entity_ids: Sequence[str] | None = None,
) -> BackfillResult:
    """Haal statistieken en volle-resolutie historie op en sla ze op.

    Args:
        config: de actieve `Config`.
        client: verbonden `HAClient`.
        store: doel-database.
        days: hoeveel dagen terug (standaard `config.backfill_days`).
        periods: welke statistiekperiodes (standaard `config.backfill_periods`).
        include_history: ook de volle-resolutie states ophalen.
        entity_ids: beperk tot deze entiteiten in plaats van discovery.
    """
    result = BackfillResult()
    days = days if days is not None else config.backfill_days
    periods = tuple(periods) if periods else tuple(config.backfill_periods)
    end = utcnow()
    start = end - timedelta(days=days)

    if entity_ids is None:
        found = await discover(config, client)
        for meta in found.meta.values():
            store.upsert_entity(meta)
        targets = found.sorted_ids()
    else:
        targets = sorted(set(entity_ids))

    # Entiteiten waarvan de interessante waarde in een attribuut zit, zoals de
    # kamertemperatuur van een thermostaat. Daarvoor moet de historie mét
    # attributen worden opgehaald. Dat is een veel zwaardere vraag, dus het
    # gebeurt alleen voor de entiteiten die het nodig hebben.
    attribute_targets: set[str] = set()
    if config.attribute_series:
        try:
            for state in await client.get_states():
                entity_id = str(state.get("entity_id") or "")
                if entity_id not in targets:
                    continue
                attrs = state.get("attributes") or {}
                if any(name in attrs for name in config.attribute_series):
                    attribute_targets.add(entity_id)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("Kon niet bepalen welke entiteiten attributen hebben: %s", exc)
    if attribute_targets:
        _LOG.info(
            "  %d entiteiten leveren meetwaarden via attributen", len(attribute_targets)
        )
    result.entities = len(targets)
    if not targets:
        result.errors.append("geen entiteiten gevonden om te backfillen")
        return result

    _LOG.info(
        "Backfill van %d entiteiten over %d dagen (%s)",
        len(targets),
        days,
        ", ".join(periods),
    )

    for period in periods:
        rows_for_period = 0
        for chunk in _chunks(targets, _STATS_CHUNK):
            try:
                data = await client.statistics_during_period(chunk, start, end, period=period)
            except Exception as exc:  # noqa: BLE001
                msg = f"statistieken {period} mislukt: {exc}"
                _LOG.warning("%s", msg)
                result.errors.append(msg)
                continue
            for entity_id, rows in (data or {}).items():
                written = store.add_statistics(str(entity_id), period, rows or [])
                rows_for_period += written
        result.periods[period] = rows_for_period
        result.statistics_rows += rows_for_period
        _LOG.info("  %-8s %6d nieuwe rijen", period, rows_for_period)

    if include_history:
        plain = [t for t in targets if t not in attribute_targets]
        result.reading_rows = await _backfill_history(
            client, store, plain, start, end, result, config=config
        )
        if attribute_targets:
            result.reading_rows += await _backfill_history(
                client,
                store,
                sorted(attribute_targets),
                start,
                end,
                result,
                config=config,
                with_attributes=True,
            )
        _LOG.info("  historie %6d nieuwe meetwaarden", result.reading_rows)

    return result


async def _backfill_history(
    client: HAClient,
    store: Store,
    targets: Sequence[str],
    start: datetime,
    end: datetime,
    result: BackfillResult,
    *,
    config: Any = None,
    with_attributes: bool = False,
) -> int:
    """Haal volle-resolutie states op via de REST history-API.

    Met `with_attributes` komen ook de attributen mee en worden de gevolgde
    numerieke attributen als eigen meetreeks weggeschreven. Dat levert veel
    meer data op, dus het gebeurt alleen voor entiteiten die het nodig hebben.
    """
    from .util import to_float

    if not targets:
        return 0
    series_names: tuple[str, ...] = ()
    if with_attributes and config is not None:
        series_names = tuple(config.attribute_series)
    # Met attributen is elk antwoord veel groter, dus in kleinere happen.
    chunk_size = 2 if with_attributes else _HISTORY_CHUNK

    total = 0
    seen_series: set[str] = set()
    for chunk in _chunks(targets, chunk_size):
        try:
            series = await client.history(chunk, start, end, minimal=not with_attributes)
        except Exception as exc:  # noqa: BLE001
            msg = f"historie mislukt voor {chunk[0]}...: {exc}"
            _LOG.warning("%s", msg)
            result.errors.append(msg)
            continue
        readings: list[Reading] = []
        for entity_series in series or []:
            # Bij minimal_response staat de entity_id alleen in het eerste item.
            entity_id = ""
            for point in entity_series or []:
                entity_id = str(point.get("entity_id") or entity_id)
                if not entity_id:
                    continue
                ts = parse_ts(point.get("last_updated")) or parse_ts(point.get("last_changed"))
                if ts is None:
                    continue
                readings.append(
                    Reading.from_state(entity_id, point.get("state"), ts, source="history")
                )
                if not series_names:
                    continue
                attrs = point.get("attributes") or {}
                for name in series_names:
                    if name not in attrs:
                        continue
                    value = to_float(attrs.get(name))
                    if value is None:
                        continue
                    series_id = f"{entity_id}#{name}"
                    if series_id not in seen_series:
                        seen_series.add(series_id)
                        unit = attrs.get("temperature_unit") if "temp" in name else None
                        if name in ("current_humidity", "humidity"):
                            unit = "%"
                        store.upsert_entity(
                            EntityMeta(
                                entity_id=series_id,
                                friendly_name=(
                                    f"{attrs.get('friendly_name') or entity_id} ({name})"
                                ),
                                unit=str(unit) if unit else None,
                                device_class="temperature" if "temp" in name else None,
                                state_class="measurement",
                                platform="attribuut",
                            )
                        )
                    readings.append(
                        Reading(
                            entity_id=series_id,
                            ts=ts,
                            value_text=str(attrs.get(name)),
                            value_num=value,
                            source="history",
                        )
                    )
        if readings:
            total += store.add_readings(readings)
    return total
