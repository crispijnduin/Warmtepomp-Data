"""Kleine hulpfuncties: tijd en getalconversie."""

from __future__ import annotations

from datetime import datetime, timezone

# Toestanden die Home Assistant gebruikt voor "geen meetwaarde".
NON_VALUES = frozenset({"unknown", "unavailable", "none", "", "null"})

# Alleen echte booleaanse toestanden worden naar 1.0/0.0 omgezet, zodat
# binary_sensors te plotten zijn. Enum-sensoren (de bedrijfsmodus heeft elf
# mogelijke waarden) blijven bewust tekst: een cijfer zou daar suggereren dat
# de waarden op een schaal liggen. Ze blijven volledig in value_text staan.
_TRUE_STATES = frozenset({"on", "true", "home", "open", "locked"})
_FALSE_STATES = frozenset({"off", "false", "not_home", "closed", "unlocked"})


def utcnow() -> datetime:
    """Huidige tijd als timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """Normaliseer een datetime naar een sorteerbare ISO-8601 UTC string.

    Naive datetimes worden als UTC gelezen. Het resultaat eindigt altijd op
    '+00:00', zodat stringsortering gelijk is aan chronologische sortering.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


# Epoch-waarden boven deze grens zijn milliseconden, niet seconden.
# 10**11 seconden is het jaar 5138, dus alles daarboven is ms.
_EPOCH_MS_THRESHOLD = 1e11


def parse_ts(value: str | float | int | datetime | None) -> datetime | None:
    """Lees een timestamp van Home Assistant uit. None bij onzin.

    Verwerkt ISO-8601 strings (ook met 'Z') en epoch-getallen. De
    statistics-API levert `start` als epoch in milliseconden; kleinere
    getallen worden als seconden gelezen.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        seconds = float(value)
        if abs(seconds) >= _EPOCH_MS_THRESHOLD:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_float(state: str | float | int | None) -> float | None:
    """Zet een Home Assistant state om naar float, of None als dat niet kan.

    Booleaanse toestanden ('on'/'off') worden 1.0/0.0 zodat ook binary_sensors
    numeriek te plotten zijn.
    """
    if state is None:
        return None
    if isinstance(state, bool):
        return 1.0 if state else 0.0
    if isinstance(state, (int, float)):
        return float(state)
    text = state.strip()
    if text.lower() in NON_VALUES:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    lowered = text.lower()
    if lowered in _TRUE_STATES:
        return 1.0
    if lowered in _FALSE_STATES:
        return 0.0
    return None
