"""Een nep-Home Assistant zodat de hele keten echt getest kan worden."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from warmtepomp.config import Config  # noqa: E402
from warmtepomp.util import iso  # noqa: E402

TOKEN = "test-token"
DEVICE_ID = "dev-flint-p40"

ENTITY_REGISTRY: list[dict[str, Any]] = [
    {
        "entity_id": "sensor.flint_cop",
        "platform": "weheat",
        "device_id": DEVICE_ID,
        "original_name": "COP",
    },
    {
        "entity_id": "sensor.flint_input_power",
        "platform": "weheat",
        "device_id": DEVICE_ID,
        "original_name": "Input power",
    },
    {
        "entity_id": "sensor.flint_electricity_used",
        "platform": "weheat",
        "device_id": DEVICE_ID,
        "original_name": "Electricity used",
    },
    {
        "entity_id": "sensor.flint_total_energy_output",
        "platform": "weheat",
        "device_id": DEVICE_ID,
        "original_name": "Total energy output",
    },
    {
        "entity_id": "sensor.flint_outside_temperature",
        "platform": "weheat",
        "device_id": DEVICE_ID,
        "original_name": "Outside temperature",
    },
    {
        "entity_id": "binary_sensor.flint_water_pump",
        "platform": "weheat",
        "device_id": DEVICE_ID,
        "original_name": "Water pump",
    },
    # Zit op het warmtepomp-device maar komt van een ander platform: moet
    # tóch worden vastgelegd (net als de kamertemperatuur in het echt).
    {
        "entity_id": "sensor.flint_room_temperature",
        "platform": "smartthings",
        "device_id": DEVICE_ID,
        "original_name": "Room temperature",
    },
    # Uitgeschakeld in Home Assistant: overslaan.
    {
        "entity_id": "sensor.flint_disabled",
        "platform": "weheat",
        "device_id": DEVICE_ID,
        "disabled_by": "integration",
    },
    # Hoort bij een ander apparaat: niet vastleggen.
    {
        "entity_id": "sensor.keukenboiler_power",
        "platform": "shelly",
        "device_id": "dev-boiler",
        "original_name": "Boiler power",
    },
]

DEVICE_REGISTRY: list[dict[str, Any]] = [
    {
        "id": DEVICE_ID,
        "name": "Flint P40 heat pump",
        "manufacturer": "Weheat",
        "model": "Flint P40 heat pump",
    },
    {"id": "dev-boiler", "name": "Keukenboiler", "manufacturer": "Shelly"},
]

_BASE = datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc)


def _state(entity_id: str, state: str, **attrs: Any) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": attrs,
        "last_changed": iso(_BASE),
        "last_updated": iso(_BASE),
    }


def initial_states() -> list[dict[str, Any]]:
    return [
        _state(
            "sensor.flint_cop",
            "4.37",
            friendly_name="Flint P40 heat pump COP",
            state_class="measurement",
        ),
        _state(
            "sensor.flint_input_power",
            "1172",
            friendly_name="Flint P40 heat pump Input power",
            unit_of_measurement="W",
            device_class="power",
            state_class="measurement",
        ),
        _state(
            "sensor.flint_electricity_used",
            "106.88",
            friendly_name="Flint P40 heat pump Electricity used",
            unit_of_measurement="kWh",
            device_class="energy",
            state_class="total_increasing",
        ),
        _state(
            "sensor.flint_total_energy_output",
            "329.79",
            friendly_name="Flint P40 heat pump Total energy output",
            unit_of_measurement="kWh",
            device_class="energy",
            state_class="total",
        ),
        _state(
            "sensor.flint_outside_temperature",
            "13.2",
            friendly_name="Flint P40 heat pump Outside temperature",
            unit_of_measurement="°C",
            device_class="temperature",
            state_class="measurement",
        ),
        _state(
            "binary_sensor.flint_water_pump",
            "on",
            friendly_name="Flint P40 heat pump Indoor unit water pump",
            device_class="running",
        ),
        _state(
            "sensor.flint_room_temperature",
            "20.0",
            friendly_name="Flint P40 heat pump Current room temperature",
            unit_of_measurement="°C",
            device_class="temperature",
        ),
        _state("sensor.keukenboiler_power", "42", friendly_name="Boiler power"),
    ]


class FakeHA:
    """Minimale Home Assistant: WebSocket-API plus de REST-endpoints die we gebruiken."""

    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_get("/api/websocket", self._websocket)
        self.app.router.add_get("/api/config", self._config)
        self.app.router.add_get("/api/history/period/{start}", self._history)
        self.states: dict[str, dict[str, Any]] = {
            s["entity_id"]: s for s in initial_states()
        }
        self.sockets: list[web.WebSocketResponse] = []
        self.subscriptions: list[tuple[web.WebSocketResponse, int]] = []
        self.commands: list[str] = []
        self.reject_auth = False
        self.statistics: dict[str, list[dict[str, Any]]] = {}
        self._runner: web.AppRunner | None = None
        self.port = 0

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        sock = next(iter(site._server.sockets))  # noqa: SLF001
        self.port = sock.getsockname()[1]

    async def stop(self) -> None:
        for ws in list(self.sockets):
            if not ws.closed:
                await ws.close()
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------- handlers

    async def _config(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"message": "unauthorized"}, status=401)
        return web.json_response(
            {
                "version": "2026.11.0",
                "location_name": "Home",
                "time_zone": "Europe/Amsterdam",
            }
        )

    async def _history(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return web.json_response({"message": "unauthorized"}, status=401)
        wanted = (request.query.get("filter_entity_id") or "").split(",")
        out: list[list[dict[str, Any]]] = []
        for entity_id in wanted:
            if entity_id not in self.states:
                continue
            series = [
                {
                    "entity_id": entity_id,
                    "state": self.states[entity_id]["state"],
                    "last_updated": iso(_BASE - timedelta(hours=offset)),
                }
                for offset in (3, 2, 1)
            ]
            out.append(series)
        return web.json_response(out)

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.11.0"})
        auth = await ws.receive_json()
        if self.reject_auth or auth.get("access_token") != TOKEN:
            await ws.send_json({"type": "auth_invalid", "message": "Invalid access token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok", "ha_version": "2026.11.0"})

        async for msg in ws:
            if msg.type is not web.WSMsgType.TEXT:
                continue
            data = msg.json()
            await self._handle(ws, data)
        return ws

    async def _handle(self, ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
        kind = data.get("type")
        msg_id = data.get("id")
        self.commands.append(str(kind))
        if kind == "get_states":
            await self._ok(ws, msg_id, list(self.states.values()))
        elif kind == "config/entity_registry/list":
            await self._ok(ws, msg_id, ENTITY_REGISTRY)
        elif kind == "config/device_registry/list":
            await self._ok(ws, msg_id, DEVICE_REGISTRY)
        elif kind == "subscribe_events":
            self.subscriptions.append((ws, int(msg_id)))
            await self._ok(ws, msg_id, None)
        elif kind == "recorder/statistics_during_period":
            wanted = data.get("statistic_ids") or []
            period = data.get("period", "hour")
            result = {
                eid: self.statistics.get(eid, self._default_stats(eid, period))
                for eid in wanted
                if eid in self.states
            }
            await self._ok(ws, msg_id, result)
        elif kind == "ping":
            await ws.send_json({"id": msg_id, "type": "pong"})
        else:
            await ws.send_json(
                {
                    "id": msg_id,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_command", "message": str(kind)},
                }
            )

    def _default_stats(self, entity_id: str, period: str) -> list[dict[str, Any]]:
        step = {"5minute": 5, "hour": 60, "day": 1440, "month": 43200}.get(period, 60)
        return [
            {
                "start": iso(_BASE - timedelta(minutes=step * (i + 1))),
                "mean": 4.0 + i * 0.1,
                "min": 3.0,
                "max": 5.0,
                "sum": 100.0 + i,
                "change": 1.5,
            }
            for i in range(3)
        ]

    async def _ok(self, ws: web.WebSocketResponse, msg_id: Any, result: Any) -> None:
        await ws.send_json({"id": msg_id, "type": "result", "success": True, "result": result})

    # --------------------------------------------------------------- helpers

    async def push_state(self, entity_id: str, state: str, when: datetime | None = None) -> None:
        """Verander een state en stuur het bijbehorende event naar alle abonnees."""
        when = when or datetime.now(timezone.utc)
        old = self.states.get(entity_id)
        new = dict(old or {"entity_id": entity_id, "attributes": {}})
        new["state"] = state
        new["last_updated"] = iso(when)
        new["last_changed"] = iso(when)
        self.states[entity_id] = new
        for ws, sub_id in list(self.subscriptions):
            if ws.closed:
                continue
            await ws.send_json(
                {
                    "id": sub_id,
                    "type": "event",
                    "event": {
                        "event_type": "state_changed",
                        "data": {"entity_id": entity_id, "old_state": old, "new_state": new},
                    },
                }
            )

    async def drop_connections(self) -> None:
        """Verbreek alle WebSockets, om herverbinden te testen."""
        self.subscriptions.clear()
        for ws in list(self.sockets):
            if not ws.closed:
                await ws.close(code=1001, message=b"bye")
        self.sockets.clear()


@pytest_asyncio.fixture
async def fake_ha() -> Any:
    server = FakeHA()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def config(fake_ha: FakeHA, tmp_path: Path) -> Config:
    return Config(
        ha_url=fake_ha.url,
        ha_token=TOKEN,
        integrations=("weheat",),
        db_path=tmp_path / "wp.sqlite",
        export_dir=tmp_path / "export",
        snapshot_interval=0.3,
        rediscover_interval=0.5,
        reconnect_min_delay=0.05,
        reconnect_max_delay=0.2,
        ws_timeout=5.0,
        backfill_days=3,
    )
