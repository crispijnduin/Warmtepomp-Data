"""Bepalen welke entiteiten bij de warmtepomp horen.

De selectie gebeurt op drie manieren, die samen worden genomen:

1. per integratie (`platform` in het entity registry, bv. `weheat`);
2. per apparaat (alle entiteiten van het warmtepomp-device, ook die welke de
   integratie zelf niet als 'eigen' meldt);
3. losse entiteiten uit de configuratie.

Omdat de lijst bij iedere ronde opnieuw wordt opgebouwd, worden sensoren die
een firmware- of integratie-update later toevoegt automatisch meegenomen.
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any

from .store import EntityMeta

_LOG = logging.getLogger(__name__)


@dataclass
class Discovered:
    """Resultaat van een discovery-ronde."""

    entity_ids: set[str] = field(default_factory=set)
    meta: dict[str, EntityMeta] = field(default_factory=dict)
    devices: dict[str, str] = field(default_factory=dict)  # device_id -> naam

    def __len__(self) -> int:
        return len(self.entity_ids)

    def sorted_ids(self) -> list[str]:
        return sorted(self.entity_ids)


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(value, pattern) for pattern in patterns)


def _device_matches(device: dict[str, Any], wanted: tuple[str, ...]) -> bool:
    """Komt dit device overeen met een van de opgegeven id's of namen?"""
    if not wanted:
        return False
    candidates = [
        str(device.get("id") or ""),
        str(device.get("name_by_user") or ""),
        str(device.get("name") or ""),
        str(device.get("model") or ""),
    ]
    return any(_matches_any(c, wanted) for c in candidates if c)


def select_entities(
    config: Any,
    entity_registry: list[dict[str, Any]],
    device_registry: list[dict[str, Any]],
    states: list[dict[str, Any]] | None = None,
) -> Discovered:
    """Stel de lijst van vast te leggen entiteiten samen.

    Args:
        config: de actieve `Config`.
        entity_registry: uitkomst van `config/entity_registry/list`.
        device_registry: uitkomst van `config/device_registry/list`.
        states: optionele huidige states, om eenheden en namen te verrijken.

    Returns:
        Een `Discovered` met de entity_id's en bijbehorende metadata.
    """
    integrations = tuple(i.lower() for i in config.integrations)
    device_names: dict[str, str] = {}
    matched_devices: set[str] = set()

    for device in device_registry:
        device_id = str(device.get("id") or "")
        if not device_id:
            continue
        name = str(device.get("name_by_user") or device.get("name") or device_id)
        device_names[device_id] = name
        if _device_matches(device, config.devices):
            matched_devices.add(device_id)

    # Devices die bij een gewenste integratie horen gelden ook als 'van ons',
    # zodat entiteiten op dat device zonder eigen platform-veld meekomen.
    for entry in entity_registry:
        platform = str(entry.get("platform") or "").lower()
        device_id = str(entry.get("device_id") or "")
        if platform and platform in integrations and device_id:
            matched_devices.add(device_id)

    state_index = {s.get("entity_id"): s for s in (states or []) if s.get("entity_id")}
    found = Discovered(devices=device_names)

    for entry in entity_registry:
        entity_id = str(entry.get("entity_id") or "")
        if not entity_id:
            continue
        platform = str(entry.get("platform") or "").lower()
        device_id = str(entry.get("device_id") or "")

        wanted = (
            (platform in integrations)
            or (device_id and device_id in matched_devices)
            or _matches_any(entity_id, config.extra_entities)
        )
        if not wanted:
            continue
        if entry.get("disabled_by"):
            _LOG.debug("%s is uitgeschakeld in Home Assistant; overgeslagen", entity_id)
            continue
        if _matches_any(entity_id, config.exclude_entities):
            continue

        found.entity_ids.add(entity_id)
        found.meta[entity_id] = _build_meta(
            entity_id,
            registry_entry=entry,
            state=state_index.get(entity_id),
            device_name=device_names.get(device_id),
        )

    # Losse entiteiten die niet in het registry staan (bv. template-sensoren
    # uit YAML) alsnog meenemen op basis van hun state.
    for entity_id, state in state_index.items():
        if entity_id in found.entity_ids:
            continue
        if not _matches_any(str(entity_id), config.extra_entities):
            continue
        if _matches_any(str(entity_id), config.exclude_entities):
            continue
        found.entity_ids.add(str(entity_id))
        found.meta[str(entity_id)] = _build_meta(str(entity_id), state=state)

    _LOG.info(
        "Discovery: %d entiteiten gevonden over %d apparaten",
        len(found.entity_ids),
        len({m.device_id for m in found.meta.values() if m.device_id}),
    )
    return found


def _build_meta(
    entity_id: str,
    *,
    registry_entry: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    device_name: str | None = None,
) -> EntityMeta:
    """Combineer registry- en state-informatie tot één EntityMeta."""
    entry = registry_entry or {}
    attrs: dict[str, Any] = dict((state or {}).get("attributes") or {})
    options = attrs.get("options")
    if not isinstance(options, list):
        options = None

    friendly = (
        entry.get("name")
        or attrs.get("friendly_name")
        or entry.get("original_name")
        or entity_id
    )
    return EntityMeta(
        entity_id=entity_id,
        friendly_name=str(friendly),
        unit=_str_or_none(attrs.get("unit_of_measurement") or entry.get("unit_of_measurement")),
        device_class=_str_or_none(
            attrs.get("device_class")
            or entry.get("device_class")
            or entry.get("original_device_class")
        ),
        state_class=_str_or_none(attrs.get("state_class")),
        device_id=_str_or_none(entry.get("device_id")),
        device_name=_str_or_none(device_name),
        platform=_str_or_none(entry.get("platform")),
        options=options,
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def discover(config: Any, client: Any) -> Discovered:
    """Haal registries en states op en bepaal de entiteitenlijst."""
    entity_registry = await client.entity_registry()
    device_registry = await client.device_registry()
    states = await client.get_states()
    return select_entities(config, entity_registry, device_registry, states)
