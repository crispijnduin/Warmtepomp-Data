"""Eenvoudige afgeleiden uit de vastgelegde data.

Niet bedoeld als volwaardig analyseplatform, maar om zonder extra tooling te
kunnen controleren dat de capture klopt en om de belangrijkste
warmtepompgetallen te zien: verbruik, opbrengst en COP.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import Store
from .util import parse_ts


def local_tz(name: str | None = None) -> tzinfo:
    """De tijdzone waarin dagen worden afgebakend.

    Home Assistant rekent zijn dag- en maandstatistieken af op lokale
    middernacht. Die momenten staan in de database als UTC, dus om een dag het
    juiste label te geven moet er terug worden gerekend naar lokale tijd.
    Zonder lokale tijdzone zou 'maandag' op zondag 23:00 beginnen.
    """
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    local = datetime.now().astimezone().tzinfo
    return local if local is not None else ZoneInfo("UTC")


def _day_label(value: str | None, tz: tzinfo) -> str | None:
    """Zet een opgeslagen UTC-timestamp om naar een lokale datum (YYYY-MM-DD)."""
    parsed = parse_ts(value)
    if parsed is None:
        return None
    return parsed.astimezone(tz).date().isoformat()

# Achtervoegsels van de Weheat-entiteiten die we voor de energiebalans gebruiken.
ELECTRICITY_SUFFIX = "_electricity_used"
OUTPUT_SUFFIX = "_total_energy_output"
COP_SUFFIX = "_cop"


@dataclass(frozen=True)
class DailyRow:
    """Eén dag aan energiebalans."""

    day: str
    electricity_kwh: float | None
    output_kwh: float | None

    @property
    def cop(self) -> float | None:
        if not self.electricity_kwh or self.output_kwh is None:
            return None
        return self.output_kwh / self.electricity_kwh

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "electricity_kwh": self.electricity_kwh,
            "output_kwh": self.output_kwh,
            "cop": self.cop,
        }


def _find_entity(store: Store, suffix: str) -> str | None:
    """Zoek de entiteit die op `suffix` eindigt (eerste treffer, alfabetisch)."""
    row = store.conn.execute(
        "SELECT entity_id FROM entities WHERE entity_id LIKE ? ORDER BY entity_id LIMIT 1",
        (f"%{suffix}",),
    ).fetchone()
    return row["entity_id"] if row else None


def _daily_change(store: Store, entity_id: str, tz: tzinfo) -> dict[str, float]:
    """Verbruik per dag uit de dag-statistieken, anders uit de meterstanden.

    De dagen worden in lokale tijd gelabeld, zodat ze overeenkomen met wat
    Home Assistant en de energieleverancier een dag noemen.
    """
    rows = store.conn.execute(
        """SELECT start, change
           FROM statistics
           WHERE entity_id = ? AND period = 'day' AND change IS NOT NULL
           ORDER BY start""",
        (entity_id,),
    ).fetchall()
    if rows:
        out: dict[str, float] = {}
        for row in rows:
            day = _day_label(row["start"], tz)
            if day is None:
                continue
            out[day] = out.get(day, 0.0) + float(row["change"])
        return out

    # Terugvalweg: het verschil tussen de laagste en hoogste meterstand per dag.
    rows = store.conn.execute(
        """SELECT ts, value_num
           FROM readings
           WHERE entity_id = ? AND value_num IS NOT NULL
           ORDER BY ts""",
        (entity_id,),
    ).fetchall()
    per_day: dict[str, list[float]] = {}
    for row in rows:
        day = _day_label(row["ts"], tz)
        if day is None:
            continue
        per_day.setdefault(day, []).append(float(row["value_num"]))
    return {day: max(values) - min(values) for day, values in per_day.items()}


def daily_energy(store: Store, tz: tzinfo | str | None = None) -> list[DailyRow]:
    """Energiebalans per dag: ingaande stroom, uitgaande warmte en COP."""
    zone = tz if isinstance(tz, tzinfo) else local_tz(tz)
    elec_id = _find_entity(store, ELECTRICITY_SUFFIX)
    out_id = _find_entity(store, OUTPUT_SUFFIX)
    elec = _daily_change(store, elec_id, zone) if elec_id else {}
    output = _daily_change(store, out_id, zone) if out_id else {}
    days = sorted(set(elec) | set(output))
    return [DailyRow(day=day, electricity_kwh=elec.get(day), output_kwh=output.get(day)) for day in days]


def seasonal_cop(store: Store, tz: tzinfo | str | None = None) -> dict[str, Any]:
    """Totaalcijfers over alle vastgelegde dagen (de SCOP tot nu toe)."""
    rows = daily_energy(store, tz)
    elec = sum(r.electricity_kwh for r in rows if r.electricity_kwh)
    output = sum(r.output_kwh for r in rows if r.output_kwh)
    return {
        "days": len(rows),
        "first_day": rows[0].day if rows else None,
        "last_day": rows[-1].day if rows else None,
        "electricity_kwh": round(elec, 3) if elec else 0.0,
        "output_kwh": round(output, 3) if output else 0.0,
        "scop": round(output / elec, 3) if elec else None,
    }


def cop_vs_outside_temperature(store: Store, bucket: float = 2.0) -> list[dict[str, Any]]:
    """Gemiddelde COP per buitentemperatuurklasse, uit de uur-statistieken.

    De COP wordt per uur uit de energietellers berekend (warmte gedeeld door
    stroom), niet uit het gemiddelde van de COP-sensor. Dat is nodig omdat de
    momentane COP in standby 0 is en tijdens ontdooien negatief; een gemiddelde
    daarover zegt niets. Alleen uren met noemenswaardig verbruik tellen mee.

    Zonder energiestatistieken valt de functie terug op de COP-sensor, waarbij
    alleen positieve waarden worden gebruikt.
    """
    temp_id = _find_entity(store, "_outside_temperature")
    if not temp_id:
        return []

    method = "energie"
    rows = _hourly_cop_from_energy(store, temp_id)
    if not rows:
        method = "cop-sensor"
        rows = _hourly_cop_from_sensor(store, temp_id)
    if not rows:
        return []

    buckets: dict[float, list[tuple[float, float, float]]] = {}
    for temp, output, elec in rows:
        key = round(temp // bucket * bucket, 1)
        buckets.setdefault(key, []).append((temp, output, elec))

    out: list[dict[str, Any]] = []
    for key, values in sorted(buckets.items()):
        total_out = sum(v[1] for v in values)
        total_elec = sum(v[2] for v in values)
        out.append(
            {
                "outside_temp_from": key,
                "outside_temp_to": round(key + bucket, 1),
                "hours": len(values),
                "method": method,
                # Gewogen naar energie: lange, zware uren wegen zwaarder dan
                # korte. Dat is wat je op de meter terugziet.
                "cop_mean": round(total_out / total_elec, 3) if total_elec else None,
                "output_kwh": round(total_out, 3),
                "electricity_kwh": round(total_elec, 3),
            }
        )
    return out


# Onder dit uurverbruik (kWh) staat de pomp praktisch stil; zulke uren
# vertekenen de COP omdat standbyverbruik geen warmte oplevert.
_MIN_HOURLY_KWH = 0.05


def _hourly_cop_from_energy(store: Store, temp_id: str) -> list[tuple[float, float, float]]:
    """(buitentemperatuur, warmte kWh, stroom kWh) per uur uit de tellers."""
    elec_id = _find_entity(store, ELECTRICITY_SUFFIX)
    out_id = _find_entity(store, OUTPUT_SUFFIX)
    if not elec_id or not out_id:
        return []
    rows = store.conn.execute(
        """
        SELECT t.mean AS temp, o.change AS output, e.change AS elec
        FROM statistics AS t
        JOIN statistics AS o ON o.entity_id = ? AND o.period = 'hour' AND o.start = t.start
        JOIN statistics AS e ON e.entity_id = ? AND e.period = 'hour' AND e.start = t.start
        WHERE t.entity_id = ? AND t.period = 'hour'
          AND t.mean IS NOT NULL AND o.change IS NOT NULL AND e.change IS NOT NULL
        ORDER BY t.start
        """,
        (out_id, elec_id, temp_id),
    ).fetchall()
    return [
        (float(r["temp"]), float(r["output"]), float(r["elec"]))
        for r in rows
        if float(r["elec"]) > _MIN_HOURLY_KWH and float(r["output"]) > 0
    ]


# Een uurgemiddelde COP onder deze waarde komt van een uur dat grotendeels in
# standby (COP 0) of ontdooien (COP negatief) is doorgebracht. Het gemiddelde
# zegt dan niets over het rendement tijdens bedrijf, dus zo'n uur gaat eruit.
_MIN_HOURLY_MEAN_COP = 1.5


def _hourly_cop_from_sensor(store: Store, temp_id: str) -> list[tuple[float, float, float]]:
    """Terugvalweg: de COP-sensor zelf, alleen de uren die echt gestookt hebben.

    Elk uur weegt even zwaar; de COP wordt als (cop, 1.0) doorgegeven zodat de
    deling in de aanroeper een gewoon gemiddelde oplevert.
    """
    cop_id = _find_entity(store, COP_SUFFIX)
    if not cop_id:
        return []
    rows = store.conn.execute(
        """
        SELECT c.mean AS cop, t.mean AS temp
        FROM statistics AS c
        JOIN statistics AS t ON t.entity_id = ? AND t.period = c.period AND t.start = c.start
        WHERE c.entity_id = ? AND c.period = 'hour'
          AND c.mean IS NOT NULL AND t.mean IS NOT NULL AND c.mean >= ?
        ORDER BY c.start
        """,
        (temp_id, cop_id, _MIN_HOURLY_MEAN_COP),
    ).fetchall()
    return [(float(r["temp"]), float(r["cop"]), 1.0) for r in rows]


# Buiten dit bereik is een buitentemperatuurmeting in Nederland niet
# geloofwaardig; de sensor in de buitenunit wordt door zon op de behuizing en
# door ontdooicycli beinvloed.
PLAUSIBLE_OUTSIDE_RANGE = (-25.0, 40.0)


def data_quality(store: Store) -> dict[str, Any]:
    """Signaleer meetwaarden die technisch niet kunnen kloppen.

    De capture slaat alles op zoals Home Assistant het levert; deze functie
    rekent niets weg maar maakt zichtbaar waar voorzichtigheid geboden is.
    """
    findings: list[dict[str, Any]] = []

    cop_id = _find_entity(store, COP_SUFFIX)
    if cop_id:
        row = store.conn.execute(
            """SELECT COUNT(*) AS n, MIN(value_num) AS lo
               FROM readings WHERE entity_id = ? AND value_num < 0""",
            (cop_id,),
        ).fetchone()
        if row and row["n"]:
            findings.append(
                {
                    "entity_id": cop_id,
                    "issue": "negatieve COP",
                    "count": int(row["n"]),
                    "worst": row["lo"],
                    "explanation": (
                        "Tijdens ontdooien draait de cyclus om: de pomp haalt warmte "
                        "uit het water. Reken COP over energietellers, niet over deze sensor."
                    ),
                }
            )

    temp_id = _find_entity(store, "_outside_temperature")
    if temp_id:
        lo, hi = PLAUSIBLE_OUTSIDE_RANGE
        row = store.conn.execute(
            """SELECT COUNT(*) AS n, MAX(value_num) AS hi, MIN(value_num) AS lo
               FROM readings
               WHERE entity_id = ? AND value_num IS NOT NULL
                 AND (value_num > ? OR value_num < ?)""",
            (temp_id, hi, lo),
        ).fetchone()
        if row and row["n"]:
            findings.append(
                {
                    "entity_id": temp_id,
                    "issue": "ongeloofwaardige buitentemperatuur",
                    "count": int(row["n"]),
                    "worst": row["hi"],
                    "explanation": (
                        "De sensor zit in de buitenunit en loopt op door zon op de "
                        "behuizing of door een ontdooicyclus. Gebruik voor "
                        "weersafhankelijke analyse een losse buitensensor."
                    ),
                }
            )

    # Een entiteit die eerder metingen gaf en nu meer dan een dag stil is, is
    # vermoedelijk uitgevallen. Entiteiten zonder enige meting worden hier niet
    # gemeld: die zijn nog nooit gaan leveren, wat een ander verhaal is, en
    # entiteiten waarvan alleen statistieken zijn ingelezen zijn geen storing.
    stale = store.conn.execute(
        """
        SELECT entity_id, MAX(ts) AS last_ts
        FROM readings
        GROUP BY entity_id
        HAVING last_ts < (SELECT MAX(ts) FROM readings)
        """
    ).fetchall()
    newest = store.conn.execute("SELECT MAX(ts) AS ts FROM readings").fetchone()
    newest_dt = parse_ts(newest["ts"]) if newest and newest["ts"] else None
    if newest_dt is not None:
        for row in stale:
            last = parse_ts(row["last_ts"])
            if last is None or (newest_dt - last).total_seconds() <= 86400:
                continue
            findings.append(
                {
                    "entity_id": row["entity_id"],
                    "issue": "levert geen data meer",
                    "count": 0,
                    "worst": row["last_ts"],
                    "explanation": (
                        "Deze entiteit gaf eerder metingen maar is nu meer dan een "
                        "dag stil. Soms terecht (een functie die niet gebruikt "
                        "wordt), soms een sensor die uitgevallen is."
                    ),
                }
            )

    open_gaps = store.conn.execute(
        "SELECT started_at, reason FROM gaps WHERE ended_at IS NULL"
    ).fetchall()
    return {
        "findings": findings,
        "open_gaps": [dict(r) for r in open_gaps],
    }
