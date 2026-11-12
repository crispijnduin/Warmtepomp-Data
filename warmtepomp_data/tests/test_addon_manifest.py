"""Controleert dat de app zoals die wordt uitgeleverd ook echt opstart.

De instellingen staan op twee plekken: in config.yaml (wat Home Assistant de
gebruiker voorschotelt) en in config.py (wat de code ermee doet). Die twee
kunnen uit elkaar lopen, en dat merk je dan pas als de app bij een gebruiker
niet start. Deze tests binden ze aan elkaar.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from warmtepomp.config import Config, load_config

ADDON_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ADDON_DIR.parent


@pytest.fixture(scope="module")
def addon_config() -> dict[str, Any]:
    return yaml.safe_load((ADDON_DIR / "config.yaml").read_text(encoding="utf-8"))


def test_repository_manifest_is_valid() -> None:
    """Zonder geldige repository.yaml laadt Home Assistant het repo niet."""
    data = yaml.safe_load((REPO_ROOT / "repository.yaml").read_text(encoding="utf-8"))
    assert set(data) >= {"name", "url", "maintainer"}
    assert data["url"].startswith("https://github.com/")


def test_addon_manifest_has_what_the_supervisor_needs(addon_config: dict[str, Any]) -> None:
    assert addon_config["slug"]
    assert addon_config["version"]
    assert addon_config["arch"], "zonder arch biedt de Supervisor de app nergens aan"
    # Zonder deze vlag krijgt de app geen SUPERVISOR_TOKEN en kan hij niet bij
    # de Home Assistant-API; dan is een eigen token nodig.
    assert addon_config["homeassistant_api"] is True
    # De database moet buiten de container staan, anders is hij na een update weg.
    assert "share:rw" in addon_config["map"]
    # Een vooraf gebouwde image zou de lokale Dockerfile overslaan.
    assert "image" not in addon_config


def test_shipped_options_are_accepted_by_the_code(
    addon_config: dict[str, Any], tmp_path: Path
) -> None:
    """De standaardinstellingen uit config.yaml moeten gewoon laden.

    Dit is precies wat er bij een verse installatie in /data/options.json staat.
    """
    options = addon_config["options"]
    path = tmp_path / "options.json"
    path.write_text(json.dumps(options), encoding="utf-8")

    cfg = load_config(path, environ={"SUPERVISOR_TOKEN": "supervisor-token"})
    assert cfg.integrations == ("weheat",)
    assert cfg.snapshot_interval == 300.0
    assert cfg.backfill_days == 400
    assert cfg.store_attributes is True
    # Leeg gelaten velden mogen de standaardwaarden niet wegdrukken.
    assert cfg.devices == ()
    assert cfg.extra_entities == ()
    # Zonder eigen URL valt de app terug op de Supervisor-proxy.
    assert cfg.ha_token == "supervisor-token"


def test_every_option_is_a_setting_the_code_knows(addon_config: dict[str, Any]) -> None:
    """Een instelling die de code niet kent, doet in de app niets.

    Home Assistant laat de gebruiker hem wel invullen, wat misleidend is.
    """
    known = {f.name for f in Config.__dataclass_fields__.values()}
    unknown = set(addon_config["options"]) - known
    assert not unknown, f"deze opties doen niets: {sorted(unknown)}"


def test_schema_covers_every_option(addon_config: dict[str, Any]) -> None:
    """Een optie zonder schema wordt door de Supervisor geweigerd."""
    assert set(addon_config["options"]) == set(addon_config["schema"])


def test_translations_describe_every_option(addon_config: dict[str, Any]) -> None:
    """Zonder vertaling ziet de gebruiker een kale sleutelnaam in de interface."""
    for language in ("nl", "en"):
        path = ADDON_DIR / "translations" / f"{language}.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        described = set(data["configuration"])
        missing = set(addon_config["options"]) - described
        assert not missing, f"{language}.yaml mist uitleg bij: {sorted(missing)}"


def test_supervisor_connection_defaults_are_right() -> None:
    """De proxy-URL's die het startscript zet moeten goed worden uitgelegd."""
    cfg = Config(
        ha_url="http://supervisor/core",
        ha_ws_url="ws://supervisor/core/websocket",
        ha_token="t",
    )
    assert cfg.rest_url == "http://supervisor/core/api"
    assert cfg.ws_url == "ws://supervisor/core/websocket"


def test_run_script_starts_the_capture() -> None:
    """Het startscript moet de capture echt starten en /data/options.json lezen."""
    run = (ADDON_DIR / "rootfs/etc/services.d/warmtepomp/run").read_text(encoding="utf-8")
    assert run.startswith("#!/usr/bin/with-contenv bashio")
    assert "exec warmtepomp" in run
    assert "/data/options.json" in run
    # De database hoort in /share, niet in de container.
    assert "/share/warmtepomp" in run


def test_dockerfile_builds_the_package() -> None:
    dockerfile = (ADDON_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG BUILD_FROM" in dockerfile
    assert "FROM ${BUILD_FROM}" in dockerfile
    # De s6-servicedefinitie moet in de image terechtkomen.
    assert "COPY rootfs /" in dockerfile
    # Zonder python3 doet pip niets op de Alpine-basis.
    assert "python3" in dockerfile


def test_version_matches_the_package() -> None:
    from warmtepomp import __version__

    addon = yaml.safe_load((ADDON_DIR / "config.yaml").read_text(encoding="utf-8"))
    assert addon["version"] == __version__, (
        "Home Assistant biedt een update aan op basis van het versienummer in "
        "config.yaml; dat moet meelopen met het pakket."
    )
