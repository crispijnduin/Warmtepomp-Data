#!/usr/bin/env python3
"""Stel de vraag op die gasverbruik naast de warmtepomp legt.

Leest de meterstanden uit data/gasmeter.csv, maakt daar perioden van en drukt
een kant-en-klaar logbook.log-blok af. Plak dat in *Developer tools → Actions*
(YAML-modus); het antwoord verschijnt in de attributen van
`sensor.warmtepomp_query_result`.

Het script bestaat omdat de perioden met elke nieuwe meterstand veranderen:
een vastgelegde query zou na de volgende opname al verouderd zijn.

    python3 scripts/gasvraag.py [pad/naar/gasmeter.csv]
    python3 scripts/gasvraag.py --ketel   # hoe lang de gasketel vrij was
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

STANDAARD = Path(__file__).resolve().parent.parent / "data" / "gasmeter.csv"
LOKAAL = ZoneInfo("Europe/Amsterdam")

WARMTE = "sensor.home_flint_p40_heat_pump_total_energy_output"
STROOM = "sensor.home_flint_p40_heat_pump_electricity_used"
KETEL = "binary_sensor.home_flint_p40_heat_pump_indoor_unit_gas_boiler_heating_allowed"

# Nederlands aardgas levert ongeveer 9,77 kWh/m³ (bovenwaarde); een HR-ketel
# zet daar ruwweg 90 % van om in nuttige warmte. Grof, maar genoeg om te zien
# welk deel van de warmte nog uit gas komt. Koken en warm water zitten er
# ongescheiden in.
KWH_PER_M3 = 8.8


def moment(rij: dict[str, str]) -> datetime:
    tijd = (rij.get("tijd") or "").strip() or "00:00"
    if tijd.count(":") == 1:
        tijd += ":00"
    lokaal = datetime.fromisoformat(f"{rij['datum'].strip()}T{tijd}")
    return lokaal.replace(tzinfo=LOKAAL).astimezone(timezone.utc)


def sql(perioden: list[tuple[str, str, str, float]]) -> str:
    regels = ",".join(
        f"('{label}','{t0}','{t1}',{gas:.3f})" for label, t0, t1, gas in perioden
    )
    return (
        f"WITH p(label,t0,t1,gas) AS (VALUES {regels}), "
        f"e(soort,id) AS (VALUES ('warmte','{WARMTE}'),('stroom','{STROOM}')), "
        "d AS (SELECT p.label AS label, p.gas AS gas, e.soort AS soort, "
        "(SELECT value_num FROM readings WHERE entity_id=e.id AND ts<=p.t1 "
        "AND value_num IS NOT NULL ORDER BY ts DESC LIMIT 1)"
        "-(SELECT value_num FROM readings WHERE entity_id=e.id AND ts<=p.t0 "
        "AND value_num IS NOT NULL ORDER BY ts DESC LIMIT 1) AS kwh "
        "FROM p JOIN e) "
        "SELECT label, gas AS gas_m3, "
        "ROUND(SUM(CASE WHEN soort='warmte' THEN kwh END),1) AS warmte_kwh, "
        "ROUND(SUM(CASE WHEN soort='stroom' THEN kwh END),1) AS stroom_kwh, "
        "ROUND(SUM(CASE WHEN soort='warmte' THEN kwh END)"
        "/SUM(CASE WHEN soort='stroom' THEN kwh END),2) AS cop, "
        f"ROUND(gas*{KWH_PER_M3},1) AS gas_kwh, "
        "ROUND(100.0*SUM(CASE WHEN soort='warmte' THEN kwh END)"
        f"/(SUM(CASE WHEN soort='warmte' THEN kwh END)+gas*{KWH_PER_M3})) AS pomp_pct "
        "FROM d GROUP BY label, gas ORDER BY label"
    )


def sql_ketel(perioden: list[tuple[str, str, str, float]]) -> str:
    """Hoeveel uur de warmtepomp de gasketel vrijgaf om bij te stoken.

    Vrijgegeven is niet hetzelfde als brandend, maar het is de enige maat die
    de pomp hierover geeft, en hij verklaart waar het gas heen ging.
    """
    regels = ",".join(f"('{label}','{t0}','{t1}')" for label, t0, t1, _ in perioden)
    return (
        f"WITH p(label,t0,t1) AS (VALUES {regels}), "
        "s AS (SELECT ts, value_text, LEAD(ts) OVER (ORDER BY ts) AS ts2 "
        f"FROM readings WHERE entity_id='{KETEL}') "
        "SELECT p.label, ROUND(SUM(CASE WHEN s.value_text='on' THEN "
        "MAX(0,(julianday(MIN(COALESCE(s.ts2,p.t1),p.t1))-julianday(MAX(s.ts,p.t0)))*24) "
        "ELSE 0 END),1) AS uren_ketel_vrij, "
        "ROUND(24*(julianday(p.t1)-julianday(p.t0)),1) AS uren_totaal "
        "FROM p JOIN s ON s.ts < p.t1 AND COALESCE(s.ts2,p.t1) > p.t0 "
        "GROUP BY p.label, p.t0, p.t1 ORDER BY p.label"
    )


def main(argv: list[str]) -> int:
    argumenten = [a for a in argv[1:] if a != "--ketel"]
    ketel = "--ketel" in argv[1:]
    pad = Path(argumenten[0]) if argumenten else STANDAARD
    if not pad.is_file():
        print(f"geen meterstanden gevonden: {pad}", file=sys.stderr)
        return 1

    with pad.open(newline="", encoding="utf-8") as fh:
        rijen = [r for r in csv.DictReader(fh) if (r.get("datum") or "").strip()]
    rijen.sort(key=moment)

    if len(rijen) < 2:
        print("minstens twee meterstanden nodig om perioden te maken.", file=sys.stderr)
        return 1

    perioden = []
    for vorige, huidige in zip(rijen, rijen[1:]):
        t0, t1 = moment(vorige), moment(huidige)
        if t1 <= t0:
            continue
        label = f"{vorige['datum'][5:]} {vorige.get('tijd', '')} - {huidige['datum'][5:]} {huidige.get('tijd', '')}"
        gas = float(huidige["stand_m3"]) - float(vorige["stand_m3"])
        perioden.append((label.strip(), t0.isoformat(), t1.isoformat(), gas))

    print("action: logbook.log")
    print("data:")
    print("  name: warmtepomp-query")
    print("  message: >-")
    print(f"    {sql_ketel(perioden) if ketel else sql(perioden)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
