"""De live capture-loop: alles wat de warmtepomp meldt wordt vastgelegd.

Werking:

* abonneren op `state_changed` → iedere wijziging komt binnen op het moment
  dat hij gebeurt, dus op volle resolutie;
* periodiek een volledige snapshot als vangnet voor gemiste events;
* periodiek opnieuw discoveren, zodat nieuwe sensoren meteen meelopen;
* bij verbindingsverlies herverbinden met toenemende wachttijd, het gat in
  de database markeren en daarna direct een snapshot nemen.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .bridge import answer_query, publish_digest, query_from_logbook_event
from .discovery import Discovered, discover
from .ha_client import HAAuthError, HAClient, HAError
from .store import EntityMeta, Reading, Store
from .util import parse_ts, utcnow

_LOG = logging.getLogger(__name__)

# Scheidt een entiteit van het attribuut dat als eigen meetreeks wordt
# vastgelegd. Een '#' komt niet voor in een echte entity_id, dus de twee zijn
# altijd uit elkaar te houden.
ATTRIBUTE_SEPARATOR = "#"


def attribute_series_id(entity_id: str, attribute: str) -> str:
    """De naam waaronder een attribuut als eigen meetreeks wordt opgeslagen."""
    return f"{entity_id}{ATTRIBUTE_SEPARATOR}{attribute}"


@dataclass
class CollectorStats:
    """Lopende tellers van één capture-sessie."""

    events_seen: int = 0
    readings_written: int = 0
    snapshots: int = 0
    reconnects: int = 0
    attribute_writes: int = 0
    digests_published: int = 0
    queries_answered: int = 0
    last_event_at: datetime | None = None
    last_error: str | None = None
    entities: int = 0
    started_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "events_seen": self.events_seen,
            "readings_written": self.readings_written,
            "snapshots": self.snapshots,
            "reconnects": self.reconnects,
            "attribute_writes": self.attribute_writes,
            "digests_published": self.digests_published,
            "queries_answered": self.queries_answered,
            "entities": self.entities,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "last_error": self.last_error,
            "started_at": self.started_at.isoformat(),
        }


class Collector:
    """Legt alle statewijzigingen van de geselecteerde entiteiten vast."""

    def __init__(self, config: Any, store: Store) -> None:
        self.config = config
        self.store = store
        self.stats = CollectorStats()
        self._targets: set[str] = set()
        self._discovered: Discovered | None = None
        self._stop = asyncio.Event()
        # Events komen binnen in een callback; de schrijfacties gaan via een
        # queue naar één consumer, zodat de WebSocket-reader nooit blokkeert.
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=10_000)
        self._dropped = 0
        # Vragen lopen via een eigen queue: een trage query mag het
        # vastleggen van meetwaarden nooit ophouden.
        self._queries: asyncio.Queue[str] = asyncio.Queue(maxsize=50)
        self._client: HAClient | None = None
        # Pseudo-entiteiten waarvan de metadata al is weggeschreven.
        self._known_series: set[str] = set()

    # ------------------------------------------------------------------ public

    def request_stop(self) -> None:
        """Vraag de loop netjes te stoppen."""
        self._stop.set()

    @property
    def targets(self) -> set[str]:
        return set(self._targets)

    async def run(self, *, max_cycles: int | None = None) -> CollectorStats:
        """Blijf data vastleggen tot er gestopt wordt.

        Args:
            max_cycles: stop na dit aantal verbindingspogingen (voor tests).
        """
        run_id = self.store.start_run("collect")
        delay = self.config.reconnect_min_delay
        cycles = 0
        try:
            while not self._stop.is_set():
                if max_cycles is not None and cycles >= max_cycles:
                    break
                cycles += 1
                client = HAClient(
                    self.config.rest_url,
                    self.config.ws_url,
                    self.config.ha_token,
                    verify_ssl=self.config.verify_ssl,
                    timeout=self.config.ws_timeout,
                )
                try:
                    await client.connect()
                    self.store.close_gaps()
                    delay = self.config.reconnect_min_delay  # verbinding staat weer
                    await self._session(client)
                except HAAuthError as exc:
                    # Een geweigerd token lost zich niet op door te wachten.
                    self.stats.last_error = str(exc)
                    self.store.open_gap(f"auth: {exc}")
                    _LOG.error("%s", exc)
                    raise
                except (TimeoutError, HAError, OSError) as exc:
                    self.stats.last_error = str(exc)
                    self.stats.reconnects += 1
                    self.store.open_gap(f"verbinding: {exc}")
                    _LOG.warning("Verbinding kwijt (%s); nieuwe poging over %.0fs", exc, delay)
                finally:
                    with contextlib.suppress(Exception):
                        await client.close()

                if self._stop.is_set() or (max_cycles is not None and cycles >= max_cycles):
                    break
                # Wachttijd verdubbelt met wat ruis, zodat herstartende
                # systemen niet allemaal op hetzelfde moment aankloppen.
                jitter = delay * 0.25 * random.random()  # noqa: S311 - geen crypto
                await self._sleep(min(delay + jitter, self.config.reconnect_max_delay))
                delay = min(delay * 2, self.config.reconnect_max_delay)
        finally:
            self.store.finish_run(
                run_id,
                events=self.stats.events_seen,
                reconnects=self.stats.reconnects,
                error=self.stats.last_error,
            )
        return self.stats

    # ----------------------------------------------------------------- session

    async def _session(self, client: HAClient) -> None:
        """Eén geslaagde verbinding: discover, snapshot, abonneer, wacht."""
        await self._rediscover(client)
        await client.subscribe_state_changed(self._on_state_changed)
        if self.config.answer_queries:
            # Via logbook.log kan iedereen met API-toegang een leesvraag aan
            # de database stellen; het antwoord komt terug als entiteit.
            await client.subscribe_events("logbook_entry", self._on_logbook_entry)
        await self._snapshot(client)

        writer = asyncio.create_task(self._writer_loop(), name="wp-writer")
        snapshotter = asyncio.create_task(
            self._periodic(self.config.snapshot_interval, lambda: self._snapshot(client)),
            name="wp-snapshot",
        )
        rediscoverer = asyncio.create_task(
            self._periodic(self.config.rediscover_interval, lambda: self._rediscover(client)),
            name="wp-rediscover",
        )
        stopper = asyncio.create_task(self._stop.wait(), name="wp-stop")
        tasks = [writer, snapshotter, rediscoverer, stopper]
        if self.config.publish_digest:
            # Meteen één keer publiceren, zodat de entiteiten er direct staan.
            await self._publish_digest(client)
            tasks.append(
                asyncio.create_task(
                    self._periodic(
                        self.config.digest_interval, lambda: self._publish_digest(client)
                    ),
                    name="wp-digest",
                )
            )
        self._client = client
        queries = asyncio.create_task(self._query_loop(), name="wp-queries")
        tasks.append(queries)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is not stopper:
                    task.result()  # laat een echte fout opborrelen
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._drain()

    def _on_state_changed(self, event: dict[str, Any]) -> None:
        """Callback uit de WebSocket-reader. Moet kort en non-blocking zijn."""
        data = event.get("data") or {}
        entity_id = data.get("entity_id")
        if not entity_id or entity_id not in self._targets:
            return
        new_state = data.get("new_state")
        if not new_state:
            return  # entiteit verwijderd
        self.stats.events_seen += 1
        self.stats.last_event_at = utcnow()
        try:
            self._queue.put_nowait(("state", new_state))
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped % 100 == 1:
                _LOG.warning(
                    "Schrijf-queue vol, %d events laten vallen; de snapshot vangt dit op",
                    self._dropped,
                )

    def _on_logbook_entry(self, event: dict[str, Any]) -> None:
        """Callback: een logboekregel kan een leesvraag zijn."""
        sql = query_from_logbook_event(event, self.config.query_marker)
        if not sql:
            return
        try:
            self._queries.put_nowait(sql)
        except asyncio.QueueFull:
            _LOG.warning("Te veel openstaande vragen; deze is overgeslagen")

    async def _query_loop(self) -> None:
        """Beantwoord binnengekomen vragen, één voor één."""
        while True:
            sql = await self._queries.get()
            try:
                if self._client is None:
                    continue
                await answer_query(self._client, self.store, sql)
                self.stats.queries_answered += 1
            except Exception:  # noqa: BLE001
                _LOG.exception("Kon vraag niet beantwoorden")
            finally:
                self._queries.task_done()

    async def _publish_digest(self, client: HAClient) -> None:
        """Zet de afgeleide cijfers in Home Assistant."""
        try:
            count = await publish_digest(client, self.store)
            self.stats.digests_published += count
        except Exception:  # noqa: BLE001
            # De samenvatting is bijzaak; het vastleggen gaat voor.
            _LOG.exception("Kon de samenvatting niet publiceren")

    async def _writer_loop(self) -> None:
        """Consumeer de queue en schrijf naar de database."""
        while True:
            kind, payload = await self._queue.get()
            try:
                if kind == "state":
                    self._write_state(payload, source="event")
                elif kind == "states":
                    self._write_states(payload, source="snapshot")
            except Exception:  # noqa: BLE001
                _LOG.exception("Kon meetwaarde niet opslaan")
            finally:
                self._queue.task_done()

    async def _drain(self) -> None:
        """Werk de queue af voordat de sessie eindigt, zodat niets verdwijnt."""
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                if kind == "state":
                    self._write_state(payload, source="event")
                elif kind == "states":
                    self._write_states(payload, source="snapshot")
            except Exception:  # noqa: BLE001
                _LOG.exception("Kon meetwaarde niet opslaan tijdens afronden")
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ writes

    def _attribute_readings(
        self, entity_id: str, attrs: dict[str, Any], ts: datetime, source: str
    ) -> list[Reading]:
        """Maak losse meetreeksen van de numerieke attributen die we volgen.

        Zo wordt de kamertemperatuur van een thermostaat een gewone meetreeks
        die net als elke sensor te bevragen, te exporteren en uit te zetten is
        tegen de aanvoertemperatuur.
        """
        from .util import to_float

        out: list[Reading] = []
        for attribute in self.config.attribute_series:
            if attribute not in attrs:
                continue
            value = to_float(attrs.get(attribute))
            if value is None:
                continue
            series_id = attribute_series_id(entity_id, attribute)
            if series_id not in self._known_series:
                self._known_series.add(series_id)
                friendly = str(attrs.get("friendly_name") or entity_id)
                unit = attrs.get("temperature_unit") if "temp" in attribute else None
                if attribute in ("current_humidity", "humidity"):
                    unit = "%"
                self.store.upsert_entity(
                    EntityMeta(
                        entity_id=series_id,
                        friendly_name=f"{friendly} ({attribute})",
                        unit=str(unit) if unit else None,
                        device_class="temperature" if "temp" in attribute else None,
                        state_class="measurement",
                        platform="attribuut",
                    )
                )
            out.append(
                Reading(
                    entity_id=series_id,
                    ts=ts,
                    value_text=str(attrs.get(attribute)),
                    value_num=value,
                    source=source,
                )
            )
        return out

    def _write_state(self, state: dict[str, Any], *, source: str) -> int:
        entity_id = str(state.get("entity_id") or "")
        if not entity_id:
            return 0
        ts = parse_ts(state.get("last_updated")) or parse_ts(state.get("last_changed")) or utcnow()
        attrs = dict(state.get("attributes") or {})
        readings = [Reading.from_state(entity_id, state.get("state"), ts, source=source)]
        readings.extend(self._attribute_readings(entity_id, attrs, ts, source))
        written = self.store.add_readings(readings)
        self.stats.readings_written += written
        if self.config.store_attributes:
            # friendly_name en unit staan al in de entities-tabel.
            attrs.pop("friendly_name", None)
            if attrs and self.store.add_attributes(entity_id, ts, attrs):
                self.stats.attribute_writes += 1
        return written

    def _write_states(self, states: list[dict[str, Any]], *, source: str) -> int:
        readings: list[Reading] = []
        for state in states:
            entity_id = str(state.get("entity_id") or "")
            if not entity_id or entity_id not in self._targets:
                continue
            ts = (
                parse_ts(state.get("last_updated"))
                or parse_ts(state.get("last_changed"))
                or utcnow()
            )
            attrs = dict(state.get("attributes") or {})
            readings.append(
                Reading.from_state(entity_id, state.get("state"), ts, source=source)
            )
            readings.extend(self._attribute_readings(entity_id, attrs, ts, source))
            if self.config.store_attributes:
                attrs.pop("friendly_name", None)
                if attrs and self.store.add_attributes(entity_id, ts, attrs):
                    self.stats.attribute_writes += 1
        written = self.store.add_readings(readings)
        self.stats.readings_written += written
        return written

    # ------------------------------------------------------------- periodieken

    async def _snapshot(self, client: HAClient) -> None:
        """Leg de huidige stand van alle entiteiten vast.

        Dit vangt events op die tijdens een herverbinding of een volle queue
        gemist zijn. Dubbele tijdstempels worden door de database genegeerd,
        dus een snapshot kost geen extra rijen als er niets veranderd is.
        """
        states = await client.get_states()
        await self._queue.put(("states", states))
        self.stats.snapshots += 1

    async def _rediscover(self, client: HAClient) -> None:
        """Werk de lijst van vast te leggen entiteiten bij."""
        found = await discover(self.config, client)
        new = found.entity_ids - self._targets
        gone = self._targets - found.entity_ids
        self._targets = set(found.entity_ids)
        self._discovered = found
        self.stats.entities = len(self._targets)
        for meta in found.meta.values():
            self.store.upsert_entity(meta)
        if new:
            _LOG.info("Nieuwe entiteiten in capture: %s", ", ".join(sorted(new)))
        if gone:
            _LOG.info("Entiteiten verdwenen uit Home Assistant: %s", ", ".join(sorted(gone)))
        if not self._targets:
            _LOG.warning(
                "Geen entiteiten gevonden voor integrations=%s devices=%s. "
                "Controleer de configuratie.",
                self.config.integrations,
                self.config.devices,
            )

    async def _periodic(self, interval: float, action: Any) -> None:
        """Voer `action` elke `interval` seconden uit tot er gestopt wordt."""
        if interval <= 0:
            await self._stop.wait()
            return
        while not self._stop.is_set():
            await self._sleep(interval)
            if self._stop.is_set():
                return
            await action()

    async def _sleep(self, seconds: float) -> None:
        """Wacht, maar word meteen wakker als er gestopt wordt."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
