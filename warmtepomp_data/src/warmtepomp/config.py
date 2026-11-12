"""Configuratie: YAML-bestand met environment-variabelen als overrule."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATHS = (
    Path("warmtepomp.yaml"),
    Path("/config/warmtepomp.yaml"),
    Path("/data/options.json"),  # Home Assistant add-on
)


class ConfigError(ValueError):
    """De configuratie is onbruikbaar."""


@dataclass(frozen=True)
class Config:
    """Alle instellingen van de capture-service."""

    # --- Home Assistant verbinding ---
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = ""
    verify_ssl: bool = True
    # Expliciete WebSocket-URL. Nodig achter de Supervisor-proxy van een
    # Home Assistant add-on, die het endpoint op /core/websocket aanbiedt in
    # plaats van op /api/websocket. Leeg betekent: afleiden uit ha_url.
    ha_ws_url: str = ""

    # --- Welke entiteiten vastleggen ---
    # Integraties waarvan alle entiteiten worden vastgelegd.
    integrations: tuple[str, ...] = ("weheat",)
    # Apparaten (device_id of exacte apparaatnaam) waarvan alles wordt vastgelegd.
    devices: tuple[str, ...] = ()
    # Losse entiteiten die er altijd bij horen (bv. buitentemperatuur, slimme meter).
    extra_entities: tuple[str, ...] = ()
    # Entiteiten die nooit worden vastgelegd (glob-patronen toegestaan).
    exclude_entities: tuple[str, ...] = ()

    # --- Opslag ---
    db_path: Path = Path("data/warmtepomp.sqlite")
    export_dir: Path = Path("data/export")
    # Attributen alleen opslaan als ze wijzigen (anders groeit de database hard).
    store_attributes: bool = True

    # --- Timing (seconden) ---
    snapshot_interval: float = 300.0
    rediscover_interval: float = 3600.0
    reconnect_min_delay: float = 2.0
    reconnect_max_delay: float = 120.0
    ws_timeout: float = 30.0
    # Een stilte langer dan dit markeert een gat in de data.
    stale_after: float = 900.0

    # --- Brug naar Home Assistant ---
    # Publiceer afgeleiden die Home Assistant zelf niet bewaart (pendelen,
    # tijd per bedrijfsmodus) terug als entiteiten, zodat ze als
    # langetermijnstatistiek bewaard blijven en opvraagbaar zijn via de API.
    publish_digest: bool = True
    digest_interval: float = 300.0
    # Beantwoord leesvragen die via logbook.log binnenkomen.
    answer_queries: bool = True
    query_marker: str = "warmtepomp-query"

    # --- Backfill ---
    backfill_periods: tuple[str, ...] = ("5minute", "hour", "day", "month")
    backfill_days: int = 400

    # Tijdzone waarin dagen worden afgebakend; leeg betekent de systeemzone.
    # Zet deze gelijk aan de tijdzone van Home Assistant.
    timezone: str = ""

    log_level: str = "INFO"
    # Alleen gevuld bij 'warmtepomp --config ...'; puur informatief.
    source_path: Path | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not self.ha_url:
            raise ConfigError("ha_url ontbreekt")
        if not self.integrations and not self.devices and not self.extra_entities:
            raise ConfigError(
                "Geef minstens één van integrations, devices of extra_entities op, "
                "anders valt er niets vast te leggen."
            )

    @property
    def ws_url(self) -> str:
        """WebSocket-endpoint: expliciet ingesteld of afgeleid van ha_url."""
        if self.ha_ws_url:
            return self.ha_ws_url
        base = self.ha_url.rstrip("/")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://") :]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://") :]
        # De Supervisor-proxy van een add-on biedt core aan op /core; daar
        # zit de WebSocket op /core/websocket, niet op /core/api/websocket.
        if base.endswith("/core"):
            return base + "/websocket"
        return base + "/api/websocket"

    @property
    def rest_url(self) -> str:
        """Basis van de REST-API, inclusief /api."""
        return self.ha_url.rstrip("/") + "/api"

    def with_overrides(self, **changes: Any) -> Config:
        """Kopie met aangepaste velden (voor tests en CLI-vlaggen)."""
        return replace(self, **changes)


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("\n", ",").split(",")]
        return tuple(p for p in parts if p)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v).strip() for v in value if str(v).strip())
    raise ConfigError(f"Kan {value!r} niet als lijst lezen")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "ja"}


_TUPLE_FIELDS = {
    "integrations",
    "devices",
    "extra_entities",
    "exclude_entities",
    "backfill_periods",
}
_BOOL_FIELDS = {"verify_ssl", "store_attributes", "publish_digest", "answer_queries"}
_PATH_FIELDS = {"db_path", "export_dir"}
_FLOAT_FIELDS = {
    "snapshot_interval",
    "rediscover_interval",
    "reconnect_min_delay",
    "reconnect_max_delay",
    "ws_timeout",
    "stale_after",
    "digest_interval",
}
_INT_FIELDS = {"backfill_days"}

# Environment-variabele -> configveld.
ENV_PREFIX = "WARMTEPOMP_"
_ENV_ALIASES = {
    "HA_URL": "ha_url",
    "HA_TOKEN": "ha_token",
    "SUPERVISOR_TOKEN": "ha_token",  # automatisch beschikbaar in een HA add-on
}


def _coerce(name: str, value: Any) -> Any:
    if name in _TUPLE_FIELDS:
        return _as_tuple(value)
    if name in _BOOL_FIELDS:
        return _as_bool(value)
    if name in _PATH_FIELDS:
        return Path(str(value)).expanduser()
    if name in _FLOAT_FIELDS:
        return float(value)
    if name in _INT_FIELDS:
        return int(value)
    return value


def _known_fields() -> set[str]:
    return {f.name for f in Config.__dataclass_fields__.values() if f.name != "source_path"}


def load_file(path: Path) -> dict[str, Any]:
    """Lees een YAML- of JSON-configbestand in als platte dict."""
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or {}  # YAML-parser leest ook JSON
    if not isinstance(data, dict):
        raise ConfigError(f"{path} moet een mapping bevatten, niet {type(data).__name__}")
    # Sta een 'warmtepomp:' toplevel toe, zoals in HA-configuratie gebruikelijk is.
    if set(data) == {"warmtepomp"} and isinstance(data["warmtepomp"], dict):
        data = data["warmtepomp"]
    return data


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Haal configwaarden uit environment-variabelen."""
    environ = os.environ if environ is None else environ
    known = _known_fields()
    out: dict[str, Any] = {}
    for key, raw in environ.items():
        if not raw:
            continue
        name: str | None = None
        if key in _ENV_ALIASES:
            name = _ENV_ALIASES[key]
        elif key.startswith(ENV_PREFIX):
            candidate = key[len(ENV_PREFIX) :].lower()
            if candidate in known:
                name = candidate
        if name is None:
            continue
        # Een expliciete WARMTEPOMP_HA_TOKEN wint van SUPERVISOR_TOKEN.
        if name == "ha_token" and key == "SUPERVISOR_TOKEN" and "ha_token" in out:
            continue
        out[name] = _coerce(name, raw)
    return out


def load_config(
    path: Path | None = None,
    *,
    environ: dict[str, str] | None = None,
    **overrides: Any,
) -> Config:
    """Bouw de configuratie op uit bestand, environment en expliciete overrides.

    Volgorde van winnen: overrides > environment > bestand > standaardwaarden.
    """
    data: dict[str, Any] = {}
    used: Path | None = None

    candidates = [path] if path is not None else list(DEFAULT_CONFIG_PATHS)
    for candidate in candidates:
        if candidate is None:
            continue
        if candidate.is_file():
            data = load_file(candidate)
            used = candidate
            break
    else:
        if path is not None:
            raise ConfigError(f"Configbestand niet gevonden: {path}")

    known = _known_fields()
    clean: dict[str, Any] = {}
    for key, value in data.items():
        name = str(key).strip().lower()
        if name not in known:
            continue  # onbekende sleutels negeren (HA add-ons voegen er zelf toe)
        if value is None or value == "":
            continue
        clean[name] = _coerce(name, value)

    clean.update(env_overrides(environ))

    for key, value in overrides.items():
        if value is None:
            continue
        if key not in known:
            raise ConfigError(f"Onbekende instelling: {key}")
        clean[key] = _coerce(key, value)

    if used is not None:
        clean["source_path"] = used
    return Config(**clean)
