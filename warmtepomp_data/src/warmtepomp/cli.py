"""Commandoregel-interface: warmtepomp <commando>."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .analyse import cop_vs_outside_temperature, daily_energy, data_quality, seasonal_cop
from .backfill import backfill
from .collector import Collector
from .config import Config, ConfigError, load_config
from .discovery import discover
from .export import entity_metadata_csv, export_all, export_readings, export_statistics, export_wide
from .ha_client import HAAuthError, HAError, open_client
from .store import Store
from .util import parse_ts

_LOG = logging.getLogger("warmtepomp")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warmtepomp",
        description="Leg alle beschikbare data van de warmtepomp vast.",
    )
    parser.add_argument("--version", action="version", version=f"warmtepomp-data {__version__}")
    parser.add_argument("--config", type=Path, help="pad naar warmtepomp.yaml")
    parser.add_argument("--db", type=Path, help="pad naar de SQLite-database")
    parser.add_argument("--ha-url", help="basis-URL van Home Assistant")
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logniveau (standaard INFO)",
    )
    parser.add_argument("--json", action="store_true", help="geef uitvoer als JSON")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="test de verbinding met Home Assistant")
    sub.add_parser("discover", help="toon welke entiteiten worden vastgelegd")

    run = sub.add_parser("run", help="leg continu alle wijzigingen vast")
    run.add_argument(
        "--once",
        action="store_true",
        help="neem één snapshot en stop (handig voor cron)",
    )
    run.add_argument(
        "--duration",
        type=float,
        help="stop automatisch na dit aantal seconden",
    )
    run.add_argument(
        "--no-backfill",
        action="store_true",
        help="sla de backfill bij het starten over",
    )

    bf = sub.add_parser("backfill", help="haal bestaande historie uit Home Assistant")
    bf.add_argument("--days", type=int, help="hoeveel dagen terug (standaard 400)")
    bf.add_argument(
        "--periods",
        help="komma-gescheiden lijst: 5minute,hour,day,month",
    )
    bf.add_argument(
        "--no-history",
        action="store_true",
        help="alleen statistieken, geen volle-resolutie states",
    )

    exp = sub.add_parser("export", help="schrijf de data naar CSV of Parquet")
    exp.add_argument(
        "--what",
        choices=["all", "readings", "wide", "statistics", "entities"],
        default="all",
    )
    exp.add_argument("--out", type=Path, help="doelmap (standaard data/export)")
    exp.add_argument(
        "--resample",
        default="5min",
        choices=["raw", "1min", "5min", "15min", "hour", "day"],
        help="tijdraster voor de brede tabel",
    )
    exp.add_argument("--since", help="alleen data vanaf deze datum (YYYY-MM-DD)")
    exp.add_argument("--parquet", action="store_true", help="ook Parquet schrijven")

    status = sub.add_parser("status", help="wat zit er in de database")
    status.add_argument("--per-entity", action="store_true", help="toon elke entiteit apart")

    rep = sub.add_parser("report", help="energie en COP uit de vastgelegde data")
    rep.add_argument(
        "--cop-curve",
        action="store_true",
        help="ook de COP per buitentemperatuurklasse",
    )

    sql = sub.add_parser("query", help="voer een eigen SELECT uit op de database")
    sql.add_argument("statement", help="SQL, alleen lezen")
    sql.add_argument("--limit", type=int, default=50, help="maximaal aantal rijen")

    return parser


def _load(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {}
    if args.db:
        overrides["db_path"] = args.db
    if args.ha_url:
        overrides["ha_url"] = args.ha_url
    if args.log_level:
        overrides["log_level"] = args.log_level
    return load_config(args.config, **overrides)


def _emit(payload: Any, as_json: bool, text: str | None = None) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    elif text is not None:
        print(text)
    else:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


# --------------------------------------------------------------------- commands


async def cmd_check(config: Config, args: argparse.Namespace) -> int:
    async with open_client(config) as client:
        info = await client.api_status()
        found = await discover(config, client)
    payload = {
        "ha_url": config.ha_url,
        "ha_version": info.get("version"),
        "location": info.get("location_name"),
        "timezone": info.get("time_zone"),
        "entities_found": len(found),
        "integrations": list(config.integrations),
    }
    _emit(
        payload,
        args.json,
        text=(
            f"Verbonden met Home Assistant {payload['ha_version']} "
            f"({payload['location']}, {payload['timezone']})\n"
            f"{len(found)} entiteiten klaar om vast te leggen."
        ),
    )
    return 0


async def cmd_discover(config: Config, args: argparse.Namespace) -> int:
    async with open_client(config) as client:
        found = await discover(config, client)
    rows = []
    for entity_id in found.sorted_ids():
        meta = found.meta[entity_id]
        rows.append(
            {
                "entity_id": entity_id,
                "friendly_name": meta.friendly_name,
                "unit": meta.unit,
                "device_class": meta.device_class,
                "state_class": meta.state_class,
                "device": meta.device_name,
            }
        )
    if args.json:
        _emit(rows, True)
    else:
        print(f"{len(rows)} entiteiten worden vastgelegd:\n")
        width = max((len(r["entity_id"]) for r in rows), default=10)
        for row in rows:
            unit = f" [{row['unit']}]" if row["unit"] else ""
            print(f"  {row['entity_id']:<{width}}  {row['friendly_name']}{unit}")
    return 0


async def cmd_run(config: Config, args: argparse.Namespace) -> int:
    with Store(config.db_path) as store:
        if args.once:
            async with open_client(config) as client:
                found = await discover(config, client)
                for meta in found.meta.values():
                    store.upsert_entity(meta)
                collector = Collector(config, store)
                collector._targets = set(found.entity_ids)  # noqa: SLF001
                states = await client.get_states()
                written = collector._write_states(states, source="snapshot")  # noqa: SLF001
            _emit(
                {"entities": len(found), "readings_written": written},
                args.json,
                text=f"Snapshot: {written} nieuwe meetwaarden van {len(found)} entiteiten.",
            )
            return 0

        if not args.no_backfill:
            try:
                async with open_client(config) as client:
                    result = await backfill(config, client, store)
                _LOG.info(
                    "Backfill bij starten: %d statistiekrijen, %d meetwaarden",
                    result.statistics_rows,
                    result.reading_rows,
                )
            except (HAError, OSError) as exc:
                _LOG.warning("Backfill bij starten mislukt (%s); ga door met live capture", exc)

        collector = Collector(config, store)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Niet elk platform ondersteunt signal handlers in de event loop.
            with contextlib.suppress(NotImplementedError, ValueError):
                loop.add_signal_handler(sig, collector.request_stop)
        if args.duration:
            loop.call_later(args.duration, collector.request_stop)

        _LOG.info("Capture gestart; database: %s", config.db_path)
        try:
            stats = await collector.run()
        except HAAuthError as exc:
            _LOG.error("%s", exc)
            return 2
        _emit(
            stats.as_dict(),
            args.json,
            text=(
                f"Gestopt. {stats.events_seen} events, "
                f"{stats.readings_written} nieuwe meetwaarden, "
                f"{stats.snapshots} snapshots, {stats.reconnects} herverbindingen."
            ),
        )
        return 0


async def cmd_backfill(config: Config, args: argparse.Namespace) -> int:
    periods = (
        tuple(p.strip() for p in args.periods.split(",") if p.strip()) if args.periods else None
    )
    with Store(config.db_path) as store:
        async with open_client(config) as client:
            result = await backfill(
                config,
                client,
                store,
                days=args.days,
                periods=periods,
                include_history=not args.no_history,
            )
    _emit(
        result.as_dict(),
        args.json,
        text=(
            f"Backfill klaar: {result.statistics_rows} statistiekrijen en "
            f"{result.reading_rows} meetwaarden van {result.entities} entiteiten."
            + (f"\nWaarschuwingen: {'; '.join(result.errors)}" if result.errors else "")
        ),
    )
    return 0


def cmd_export(config: Config, args: argparse.Namespace) -> int:
    out_dir = args.out or config.export_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    since = None
    if args.since:
        since = parse_ts(args.since) or parse_ts(f"{args.since}T00:00:00+00:00")
        if since is None:
            raise SystemExit(f"Onleesbare datum: {args.since}")
    with Store(config.db_path) as store:
        if args.what == "all":
            results = export_all(store, out_dir, resample=args.resample, parquet=args.parquet)
            results["entities"] = entity_metadata_csv(store, out_dir)
        elif args.what == "readings":
            results = {
                "readings": export_readings(store, out_dir, start=since, parquet=args.parquet)
            }
        elif args.what == "wide":
            results = {
                "wide": export_wide(
                    store, out_dir, resample=args.resample, start=since, parquet=args.parquet
                )
            }
        elif args.what == "statistics":
            results = {"statistics": export_statistics(store, out_dir, parquet=args.parquet)}
        else:
            results = {"entities": entity_metadata_csv(store, out_dir)}
    payload = {name: result.as_dict() for name, result in results.items()}
    if args.json:
        _emit(payload, True)
    else:
        for name, result in results.items():
            for path in result.files:
                print(f"  {name:<11} {result.rows:>8} rijen  -> {path}")
    return 0


def cmd_status(config: Config, args: argparse.Namespace) -> int:
    if not config.db_path.is_file():
        print(f"Nog geen database op {config.db_path}. Start met: warmtepomp run")
        return 1
    with Store(config.db_path) as store:
        stats = store.stats()
        per_entity = store.per_entity_stats() if args.per_entity else []
    if args.json:
        _emit({"database": stats, "entities": per_entity}, True)
        return 0
    size_mb = stats["db_bytes"] / 1_048_576
    print(f"Database   {stats['db_path']} ({size_mb:.1f} MB)")
    print(f"Entiteiten {stats['entities']}")
    print(f"Metingen   {stats['readings']:,}".replace(",", "."))
    print(f"Periode    {stats['first_reading']} .. {stats['last_reading']}")
    print(f"Attributen {stats['attributes']:,}".replace(",", "."))
    print(f"Statistiek {stats['statistics']:,}".replace(",", "."))
    print(f"Sessies    {stats['runs']}   open gaten: {stats['open_gaps']}")
    if per_entity:
        print("\nPer entiteit:")
        width = max(len(r["entity_id"]) for r in per_entity)
        for row in per_entity:
            print(
                f"  {row['entity_id']:<{width}}  {row['readings']:>7} metingen  "
                f"{row['first_ts']} .. {row['last_ts']}"
            )
    return 0


def cmd_report(config: Config, args: argparse.Namespace) -> int:
    with Store(config.db_path) as store:
        tz = config.timezone or None
        rows = daily_energy(store, tz)
        total = seasonal_cop(store, tz)
        curve = cop_vs_outside_temperature(store) if args.cop_curve else []
        quality = data_quality(store)
    payload = {
        "daily": [r.as_dict() for r in rows],
        "total": total,
        "cop_vs_outside_temperature": curve,
        "quality": quality,
    }
    if args.json:
        _emit(payload, True)
        return 0
    print(f"{'Dag':<12}{'Stroom kWh':>12}{'Warmte kWh':>12}{'COP':>8}")
    for row in rows:
        elec = f"{row.electricity_kwh:.2f}" if row.electricity_kwh is not None else "-"
        out = f"{row.output_kwh:.2f}" if row.output_kwh is not None else "-"
        cop = f"{row.cop:.2f}" if row.cop is not None else "-"
        print(f"{row.day:<12}{elec:>12}{out:>12}{cop:>8}")
    scop = f"{total['scop']:.2f}" if total["scop"] else "-"
    print(
        f"\nTotaal over {total['days']} dagen: "
        f"{total['electricity_kwh']:.1f} kWh stroom, "
        f"{total['output_kwh']:.1f} kWh warmte, SCOP {scop}"
    )
    if curve:
        method = curve[0].get("method", "energie")
        toelichting = {
            "energie": "berekend uit de energietellers",
            "cop-sensor": "uit de COP-sensor; minder betrouwbaar, "
            "backfill de uur-energiestatistieken voor een beter beeld",
        }.get(method, method)
        print(f"\nCOP per buitentemperatuur ({toelichting}):")
        print(f"{'Buitentemp':<14}{'Uren':>6}{'COP':>8}")
        for bucket in curve:
            label = f"{bucket['outside_temp_from']:.0f} .. {bucket['outside_temp_to']:.0f} °C"
            cop = f"{bucket['cop_mean']:.2f}" if bucket["cop_mean"] is not None else "-"
            print(f"{label:<14}{bucket['hours']:>6}{cop:>8}")
    if quality["findings"]:
        print("\nLet op bij deze data:")
        for finding in quality["findings"]:
            count = f" ({finding['count']}x)" if finding["count"] else ""
            print(f"  - {finding['entity_id']}: {finding['issue']}{count}")
            print(f"    {finding['explanation']}")
    return 0


def cmd_query(config: Config, args: argparse.Namespace) -> int:
    statement = args.statement.strip().rstrip(";")
    lowered = statement.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise SystemExit("Alleen SELECT- of WITH-queries zijn toegestaan.")
    with Store(config.db_path) as store:
        cursor = store.conn.execute(statement)
        rows = cursor.fetchmany(args.limit)
        columns = [d[0] for d in cursor.description or []]
    if args.json:
        _emit([dict(zip(columns, row, strict=True)) for row in rows], True)
        return 0
    if not rows:
        print("(geen rijen)")
        return 0
    widths = [
        max(len(str(col)), *(len(str(row[i])) for row in rows)) for i, col in enumerate(columns)
    ]
    print("  ".join(str(c).ljust(w) for c, w in zip(columns, widths, strict=True)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)))
    return 0


# ------------------------------------------------------------------- dispatcher

_ASYNC = {"check": cmd_check, "discover": cmd_discover, "run": cmd_run, "backfill": cmd_backfill}
_SYNC = {"export": cmd_export, "status": cmd_status, "report": cmd_report, "query": cmd_query}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = _load(args)
    except ConfigError as exc:
        print(f"Configuratiefout: {exc}", file=sys.stderr)
        return 2
    _setup_logging(args.log_level or config.log_level)

    try:
        if args.command in _ASYNC:
            return asyncio.run(_ASYNC[args.command](config, args))
        return _SYNC[args.command](config, args)
    except KeyboardInterrupt:
        print("\nAfgebroken.", file=sys.stderr)
        return 130
    except HAAuthError as exc:
        print(f"Authenticatie mislukt: {exc}", file=sys.stderr)
        return 2
    except HAError as exc:
        print(f"Home Assistant: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Verbindingsfout: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
