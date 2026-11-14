#!/usr/bin/env python3
"""Reken het gasverbruik uit tussen opeenvolgende meterstanden.

De standen zelf staan in data/gasmeter.csv; verbruik is een verschil en wordt
daarom niet bewaard maar hier uitgerekend.

    python3 scripts/gasverbruik.py [pad/naar/gasmeter.csv]
"""

from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

STANDAARD = Path(__file__).resolve().parent.parent / "data" / "gasmeter.csv"


def moment(rij: dict[str, str]) -> datetime:
    """Datum en tijd samen. Een lege tijd telt als middernacht."""
    tijd = (rij.get("tijd") or "").strip() or "00:00"
    if tijd.count(":") == 1:
        tijd += ":00"
    return datetime.fromisoformat(f"{rij['datum'].strip()}T{tijd}")


def main(argv: list[str]) -> int:
    pad = Path(argv[1]) if len(argv) > 1 else STANDAARD
    if not pad.is_file():
        print(f"geen meterstanden gevonden: {pad}", file=sys.stderr)
        return 1

    with pad.open(newline="", encoding="utf-8") as fh:
        rijen = [r for r in csv.DictReader(fh) if (r.get("datum") or "").strip()]
    rijen.sort(key=moment)

    if len(rijen) < 2:
        print(f"{len(rijen)} stand vastgelegd; vanaf twee standen valt er verbruik te melden.")
        return 0

    totaal_m3 = 0.0
    totaal_dagen = 0.0
    for vorige, huidige in zip(rijen, rijen[1:]):
        dagen = (moment(huidige) - moment(vorige)).total_seconds() / 86400
        m3 = float(huidige["stand_m3"]) - float(vorige["stand_m3"])
        heen = f"{vorige['datum']} {(vorige.get('tijd') or '').strip():>5}".rstrip()
        terug = f"{huidige['datum']} {(huidige.get('tijd') or '').strip():>5}".rstrip()
        if dagen <= 0:
            print(f"{heen} → {terug}: tijdstippen lopen niet op, overgeslagen")
            continue
        totaal_m3 += m3
        totaal_dagen += dagen
        print(f"{heen} → {terug}: {m3:7.3f} m³ in {dagen:6.2f} dagen = {m3 / dagen:.3f} m³/dag")

    if totaal_dagen:
        print(
            f"{'totaal':<36}{totaal_m3:7.3f} m³ in {totaal_dagen:6.2f} dagen "
            f"= {totaal_m3 / totaal_dagen:.3f} m³/dag"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
