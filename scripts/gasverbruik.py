#!/usr/bin/env python3
"""Reken het gasverbruik uit tussen opeenvolgende meterstanden.

De standen zelf staan in data/gasmeter.csv; verbruik is een verschil en wordt
daarom niet bewaard maar hier uitgerekend.

    python3 scripts/gasverbruik.py [pad/naar/gasmeter.csv]
"""

from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path

STANDAARD = Path(__file__).resolve().parent.parent / "data" / "gasmeter.csv"


def main(argv: list[str]) -> int:
    pad = Path(argv[1]) if len(argv) > 1 else STANDAARD
    if not pad.is_file():
        print(f"geen meterstanden gevonden: {pad}", file=sys.stderr)
        return 1

    with pad.open(newline="", encoding="utf-8") as fh:
        rijen = [r for r in csv.DictReader(fh) if r.get("datum")]
    rijen.sort(key=lambda r: r["datum"])

    if len(rijen) < 2:
        n = len(rijen)
        print(f"{n} stand vastgelegd; vanaf twee standen valt er verbruik te melden.")
        return 0

    totaal_m3 = 0.0
    totaal_dagen = 0
    for vorige, huidige in zip(rijen, rijen[1:]):
        dagen = (date.fromisoformat(huidige["datum"]) - date.fromisoformat(vorige["datum"])).days
        m3 = float(huidige["stand_m3"]) - float(vorige["stand_m3"])
        if dagen <= 0:
            print(f"{vorige['datum']} → {huidige['datum']}: datums lopen niet op, overgeslagen")
            continue
        totaal_m3 += m3
        totaal_dagen += dagen
        print(
            f"{vorige['datum']} → {huidige['datum']}: "
            f"{m3:7.3f} m³ in {dagen:3d} dagen = {m3 / dagen:.3f} m³/dag"
        )

    if totaal_dagen:
        print(
            f"{'totaal':<24}{totaal_m3:7.3f} m³ in {totaal_dagen:3d} dagen "
            f"= {totaal_m3 / totaal_dagen:.3f} m³/dag"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
