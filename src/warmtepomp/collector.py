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

from .discovery import Discovered, discover
from .ha_client import HAAuthError, HAClient, HAError
from .store import Reading, Store
from .util import parse_ts, utcnow

_LOG = logging.getLogger(__name__)


@dataclass
class CollectorStats:
    """Lopende tellers van één capture-sessie."""

    events_seen: int = 0
    readings_written: int = 0
    snapshots: int = 0
    reconnects: int = 0
    attribute_writes: int = 0
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

    def _write_state(self, state: dict[str, Any], *, source: str) -> int:
        entity_id = str(state.get("entity_id") or "")
        if not entity_id:
            return 0
        ts = parse_ts(state.get("last_updated")) or parse_ts(state.get("last_changed")) or utcnow()
        written = self.store.add_readings(
            [Reading.from_state(entity_id, state.get("state"), ts, source=source)]
        )
        self.stats.readings_written += written
        if self.config.store_attributes:
            attrs = dict(state.get("attributes") or {})
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
            readings.append(
                Reading.from_state(entity_id, state.get("state"), ts, source=source)
            )
            if self.config.store_attributes:
                attrs = dict(state.get("attributes") or {})
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
