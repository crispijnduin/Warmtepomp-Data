"""Client voor de Home Assistant WebSocket- en REST-API.

De WebSocket-API wordt gebruikt om live op `state_changed` te abonneren: zo
wordt elke wijziging vastgelegd op het moment dat hij gebeurt, zonder de
afrondingsverliezen van periodiek pollen. De REST-API dient als vangnet voor
de historie binnen de recorder-bewaartermijn.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import datetime
from typing import Any

import aiohttp

from .util import iso

_LOG = logging.getLogger(__name__)


class HAError(RuntimeError):
    """Een fout in het gesprek met Home Assistant."""


class HAAuthError(HAError):
    """Het access token werd geweigerd."""


class HAClient:
    """Verbinding met één Home Assistant-installatie.

    Gebruik als async context manager:

        async with HAClient(url, ws_url, token) as client:
            states = await client.get_states()
    """

    def __init__(
        self,
        rest_url: str,
        ws_url: str,
        token: str,
        *,
        verify_ssl: bool = True,
        timeout: float = 30.0,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not token:
            raise HAAuthError(
                "Geen access token. Zet WARMTEPOMP_HA_TOKEN of ha_token in de config."
            )
        self.rest_url = rest_url.rstrip("/")
        self.ws_url = ws_url
        self._token = token
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._session = session
        self._owns_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._subscriptions: dict[int, Callable[[dict[str, Any]], None]] = {}
        self._reader: asyncio.Task[None] | None = None
        self.ha_version: str | None = None

    # ------------------------------------------------------------- lifecycle

    async def __aenter__(self) -> HAClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                headers={"Authorization": f"Bearer {self._token}"},
            )
            self._owns_session = True
        return self._session

    async def connect(self) -> None:
        """Open de WebSocket en handel de authenticatie af."""
        if self._ws is not None and not self._ws.closed:
            return
        connector_kwargs: dict[str, Any] = {}
        if not self._verify_ssl:
            connector_kwargs["ssl"] = False
        self._ws = await self.session.ws_connect(
            self.ws_url, heartbeat=30, **connector_kwargs
        )
        # Home Assistant stuurt eerst auth_required, dan verwacht het het token.
        first = await self._ws.receive_json(timeout=self._timeout)
        if first.get("type") != "auth_required":
            raise HAError(f"Onverwacht eerste bericht: {first!r}")
        self.ha_version = first.get("ha_version")
        await self._ws.send_json({"type": "auth", "access_token": self._token})
        reply = await self._ws.receive_json(timeout=self._timeout)
        if reply.get("type") == "auth_invalid":
            raise HAAuthError(
                f"Home Assistant weigert het token: {reply.get('message', 'auth_invalid')}"
            )
        if reply.get("type") != "auth_ok":
            raise HAError(f"Authenticatie mislukt: {reply!r}")
        self.ha_version = reply.get("ha_version", self.ha_version)
        self._msg_id = 0
        self._pending.clear()
        self._subscriptions.clear()
        self._reader = asyncio.create_task(self._read_loop(), name="ha-ws-reader")
        _LOG.info("Verbonden met Home Assistant %s", self.ha_version)

    async def close(self) -> None:
        """Sluit de WebSocket en, als wij hem openden, de HTTP-sessie."""
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            # De reader eindigt altijd met CancelledError of de fout die hem
            # al had geveld; beide zijn hier niets om op te treden.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        self._fail_pending(HAError("verbinding gesloten"))
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------- websocket

    async def _read_loop(self) -> None:
        """Lees berichten en route ze naar resultaten of abonnementen."""
        ws = self._ws
        assert ws is not None
        try:
            async for msg in ws:
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
                    continue
                try:
                    data = msg.json()
                except ValueError:
                    _LOG.warning("Onleesbaar bericht genegeerd")
                    continue
                self._dispatch(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("Lees-loop gestopt: %s", exc)
            self._fail_pending(HAError(f"verbinding verbroken: {exc}"))
            return
        self._fail_pending(HAError("verbinding verbroken door Home Assistant"))

    def _dispatch(self, data: dict[str, Any]) -> None:
        kind = data.get("type")
        msg_id = data.get("id")
        if kind == "result":
            future = self._pending.pop(int(msg_id), None) if msg_id is not None else None
            if future is None or future.done():
                return
            if data.get("success"):
                future.set_result(data.get("result"))
            else:
                error = data.get("error") or {}
                future.set_exception(
                    HAError(
                        f"{error.get('code', 'unknown')}: {error.get('message', 'geen melding')}"
                    )
                )
        elif kind == "event" and msg_id is not None:
            handler = self._subscriptions.get(int(msg_id))
            if handler is not None:
                try:
                    handler(data.get("event") or {})
                except Exception:  # noqa: BLE001
                    _LOG.exception("Fout in event-handler")
        elif kind == "pong":
            future = self._pending.pop(int(msg_id), None) if msg_id is not None else None
            if future is not None and not future.done():
                future.set_result(None)

    def _fail_pending(self, exc: Exception) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    async def send_command(self, payload: dict[str, Any], *, timeout: float | None = None) -> Any:
        """Stuur een WebSocket-commando en wacht op het resultaat."""
        ws = self._ws
        if ws is None or ws.closed:
            raise HAError("niet verbonden met Home Assistant")
        self._msg_id += 1
        msg_id = self._msg_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            await ws.send_json({"id": msg_id, **payload})
            return await asyncio.wait_for(future, timeout or self._timeout)
        except TimeoutError as exc:
            self._pending.pop(msg_id, None)
            raise HAError(f"time-out op commando {payload.get('type')!r}") from exc
        except Exception:
            self._pending.pop(msg_id, None)
            raise

    async def subscribe_state_changed(
        self, handler: Callable[[dict[str, Any]], None]
    ) -> int:
        """Abonneer op alle state_changed-events. Geeft het subscription-id."""
        ws = self._ws
        if ws is None or ws.closed:
            raise HAError("niet verbonden met Home Assistant")
        self._msg_id += 1
        msg_id = self._msg_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        # Registreer de handler vóór het versturen: Home Assistant kan al een
        # event sturen voordat het 'result' van de subscribe binnen is.
        self._subscriptions[msg_id] = handler
        try:
            await ws.send_json(
                {"id": msg_id, "type": "subscribe_events", "event_type": "state_changed"}
            )
            await asyncio.wait_for(future, self._timeout)
        except Exception:
            self._subscriptions.pop(msg_id, None)
            self._pending.pop(msg_id, None)
            raise
        return msg_id

    # ----------------------------------------------------------- registries

    async def get_states(self) -> list[dict[str, Any]]:
        """Alle huidige states (via WebSocket)."""
        result = await self.send_command({"type": "get_states"})
        return list(result or [])

    async def entity_registry(self) -> list[dict[str, Any]]:
        result = await self.send_command({"type": "config/entity_registry/list"})
        return list(result or [])

    async def device_registry(self) -> list[dict[str, Any]]:
        result = await self.send_command({"type": "config/device_registry/list"})
        return list(result or [])

    async def statistics_during_period(
        self,
        entity_ids: Sequence[str],
        start: datetime,
        end: datetime | None = None,
        period: str = "hour",
        *,
        types: Sequence[str] = ("mean", "min", "max", "state", "sum", "change"),
    ) -> dict[str, list[dict[str, Any]]]:
        """Langetermijnstatistieken uit de recorder ophalen."""
        payload: dict[str, Any] = {
            "type": "recorder/statistics_during_period",
            "start_time": iso(start),
            "statistic_ids": list(entity_ids),
            "period": period,
            "types": list(types),
        }
        if end is not None:
            payload["end_time"] = iso(end)
        result = await self.send_command(payload, timeout=max(self._timeout, 120.0))
        return dict(result or {})

    # ----------------------------------------------------------------- REST

    async def rest_get(self, path: str, **params: Any) -> Any:
        """GET op de REST-API. `path` is relatief aan /api."""
        url = f"{self.rest_url}/{path.lstrip('/')}"
        kwargs: dict[str, Any] = {"params": {k: v for k, v in params.items() if v is not None}}
        if not self._verify_ssl:
            kwargs["ssl"] = False
        async with self.session.get(
            url, headers={"Authorization": f"Bearer {self._token}"}, **kwargs
        ) as resp:
            if resp.status == 401:
                raise HAAuthError("REST-API weigert het token (401)")
            if resp.status >= 400:
                body = (await resp.text())[:400]
                raise HAError(f"GET {path} gaf {resp.status}: {body}")
            return await resp.json()

    async def history(
        self,
        entity_ids: Sequence[str],
        start: datetime,
        end: datetime | None = None,
        *,
        minimal: bool = True,
    ) -> list[list[dict[str, Any]]]:
        """Volle-resolutie historie uit de recorder (binnen de bewaartermijn)."""
        params: dict[str, Any] = {"filter_entity_id": ",".join(entity_ids)}
        if end is not None:
            params["end_time"] = iso(end)
        if minimal:
            params["minimal_response"] = ""
        result = await self.rest_get(f"history/period/{iso(start)}", **params)
        return list(result or [])

    async def api_status(self) -> dict[str, Any]:
        """Eenvoudige verbindingscheck via de REST-API."""
        return dict(await self.rest_get("config"))


@contextlib.asynccontextmanager
async def open_client(config: Any) -> AsyncIterator[HAClient]:
    """Open een HAClient vanuit een Config-object."""
    client = HAClient(
        config.rest_url,
        config.ws_url,
        config.ha_token,
        verify_ssl=config.verify_ssl,
        timeout=config.ws_timeout,
    )
    try:
        await client.connect()
        yield client
    finally:
        await client.close()
