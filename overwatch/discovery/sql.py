"""Discover what a SQL database can tell Overwatch: tables, change logs, logs, metrics, DB health views."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import create_engine, inspect, text

TS_TYPES = ("timestamp", "datetime", "date")
LATENCY = re.compile(r"(p9\d|latency|duration|response_time).*?(_ms|_seconds|_s)?$")
ERRORS = re.compile(r"error_?rate|errors?_?(pct|ratio)?$|failure_rate")


def sqlalchemy_url(url: str) -> str:
    """Accept plain postgres URLs and use the psycopg driver Overwatch ships with."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _ts_column(cols: list[dict[str, Any]]) -> str | None:
    for c in cols:
        if any(t in str(c["type"]).lower() for t in TS_TYPES) and re.search(r"^(ts|time|timestamp|created|created_at|at|date)$|_at$|time", c["name"]):
            return c["name"]
    return next((c["name"] for c in cols if any(t in str(c["type"]).lower() for t in TS_TYPES)), None)


def discover_sql(name: str, url: str, max_tables: int = 40) -> dict[str, Any]:
    engine = create_engine(sqlalchemy_url(url), pool_pre_ping=True)
    dialect = engine.dialect.name
    insp = inspect(engine)
    schemas = ["public"] if dialect == "postgresql" else [None]
    tables: list[dict[str, Any]] = []
    for schema in schemas:
        for t in insp.get_table_names(schema=schema)[:max_tables]:
            cols = [{"name": c["name"], "type": str(c["type"]).lower()} for c in insp.get_columns(t, schema=schema)]
            tables.append({"name": t, "columns": cols})

    rows_estimate: dict[str, int] = {}
    pg_health: dict[str, bool] = {}
    with engine.connect() as conn:
        if dialect == "postgresql":
            for r in conn.execute(text("SELECT relname, reltuples::bigint FROM pg_class WHERE relkind = 'r'")):
                rows_estimate[r[0]] = int(r[1])
            pg_health["pg_stat_statements"] = bool(conn.execute(
                text("SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'")).scalar())
            try:
                conn.execute(text("SELECT count(*) FROM pg_stat_activity"))
                pg_health["pg_stat_activity"] = True
            except Exception:
                pg_health["pg_stat_activity"] = False

        roles: dict[str, dict[str, Any]] = {}
        for t in tables:
            names = {c["name"] for c in t["columns"]}
            ts = _ts_column(t["columns"])
            if not ts:
                continue
            if "service" in names and ({"version"} & names or {"description"} & names) and "message" not in names:
                roles.setdefault("changes", {"table": t["name"], "ts": ts})
            elif {"level", "message"} <= names or ({"severity", "message"} <= names):
                roles.setdefault("logs", {"table": t["name"], "ts": ts,
                                          "level": "level" if "level" in names else "severity"})
            elif "service" in names:
                latency = next((c["name"] for c in t["columns"] if LATENCY.search(c["name"])), None)
                errors = next((c["name"] for c in t["columns"] if ERRORS.search(c["name"])), None)
                if latency or errors:
                    services = [r[0] for r in conn.execute(text(
                        f"SELECT DISTINCT service FROM {t['name']} WHERE {ts} > now() - interval '1 day' LIMIT 20"
                        if dialect == "postgresql" else f"SELECT DISTINCT service FROM {t['name']} LIMIT 20"))]
                    roles.setdefault("metrics", {"table": t["name"], "ts": ts, "latency": latency,
                                                 "errors": errors, "services": [s for s in services if s]})
    engine.dispose()

    # Catalog text for the Investigator.
    lines = [f"{name} ({'PostgreSQL' if dialect == 'postgresql' else dialect} SQL):"]
    for t in tables:
        cols = ", ".join(f"{c['name']} {c['type'].split('(')[0]}" for c in t["columns"][:20])
        est = f"  (~{rows_estimate[t['name']]:,} rows)" if rows_estimate.get(t["name"], 0) > 0 else ""
        lines.append(f"  {t['name']}({cols}){est}")
    if pg_health.get("pg_stat_activity"):
        lines.append("  Postgres health views: pg_stat_activity, pg_locks, pg_indexes, pg_stat_user_tables"
                     + (", pg_stat_statements" if pg_health.get("pg_stat_statements") else ""))

    checks, evidence = [], []
    pg = dialect == "postgresql"
    window = "now() - interval '40 seconds'" if pg else "datetime('now', '-40 seconds')"
    if m := roles.get("metrics"):
        for svc in m["services"]:
            key = svc.replace("-", "_").replace(".", "_")
            if m["latency"]:
                unit = "_ms" if m["latency"].endswith("_ms") else ""
                checks.append({
                    "name": f"{key}_latency{unit}", "service": svc, "source": name,
                    "query": f"SELECT avg({m['latency']}) AS value FROM {m['table']} "
                             f"WHERE service = '{svc}' AND {m['ts']} > {window}",
                    "relative_factor": 4, "relative_min": 100 if unit else 0.1, "no_data": "alert",
                    "description": f"{svc} {m['latency']}, last 40s", "severity": "high",
                    "why": f"{m['table']}.{m['latency']}: alerts at 4x its own normal, no threshold to guess",
                })
            if m["errors"]:
                checks.append({
                    "name": f"{key}_error_rate", "service": svc, "source": name,
                    "query": f"SELECT avg({m['errors']}) AS value FROM {m['table']} "
                             f"WHERE service = '{svc}' AND {m['ts']} > {window}",
                    "threshold": 0.05, "description": f"{svc} {m['errors']} (0-1), last 40s", "severity": "high",
                    "why": f"{m['table']}.{m['errors']}: alerts above 5% errors",
                })
        evidence.append({
            "name": f"{name}_service_health", "label": "Latency and errors per service, last 2 minutes vs last hour",
            "source": name,
            "query": (f"SELECT service, round((avg({m['latency'] or 'NULL'}) FILTER (WHERE {m['ts']} > now() - interval '2 minutes'))::numeric, 1) AS latency_now, "
                      f"round(avg({m['latency'] or 'NULL'})::numeric, 1) AS latency_hour, "
                      f"round((avg({m['errors'] or 'NULL'}) FILTER (WHERE {m['ts']} > now() - interval '2 minutes'))::numeric, 3) AS errors_now "
                      f"FROM {m['table']} WHERE {m['ts']} > now() - interval '1 hour' GROUP BY 1 ORDER BY 1")
            if pg else f"SELECT service, avg({m['latency'] or 'NULL'}), avg({m['errors'] or 'NULL'}) FROM {m['table']} GROUP BY 1",
        })
    if c := roles.get("changes"):
        evidence.append({
            "name": f"{name}_changes", "label": f"Changes in the last 30 minutes ({c['table']})", "source": name,
            "query": f"SELECT * FROM {c['table']} WHERE {c['ts']} > "
                     + ("now() - interval '30 minutes'" if pg else "datetime('now', '-30 minutes')")
                     + f" ORDER BY {c['ts']} DESC LIMIT 15",
        })
    if lg := roles.get("logs"):
        evidence.append({
            "name": f"{name}_top_errors", "label": f"Top error messages in the last 5 minutes ({lg['table']})", "source": name,
            "query": f"SELECT left(message, 160) AS message, count(*) AS n FROM {lg['table']} "
                     f"WHERE upper({lg['level']}) IN ('ERROR', 'FATAL', 'CRITICAL') AND {lg['ts']} > now() - interval '5 minutes' "
                     "GROUP BY 1 ORDER BY n DESC LIMIT 10" if pg else
                     f"SELECT message, count(*) AS n FROM {lg['table']} GROUP BY 1 ORDER BY n DESC LIMIT 10",
        })
    if pg_health.get("pg_stat_activity"):
        evidence.append({
            "name": f"{name}_db_activity", "label": f"Busy or waiting sessions on {name}", "source": name,
            "query": "SELECT application_name, state, wait_event_type, wait_event, left(query, 120) AS query, "
                     "now() - query_start AS running_for FROM pg_stat_activity "
                     "WHERE state <> 'idle' AND pid <> pg_backend_pid() ORDER BY query_start LIMIT 10",
        })

    notes = []
    if pg and not pg_health.get("pg_stat_statements"):
        notes.append(f"{name}: enable the pg_stat_statements extension to let Overwatch see slow queries.")
    if pg and not pg_health.get("pg_stat_activity"):
        notes.append(f"{name}: grant pg_read_all_stats to Overwatch's user to see sessions and locks.")
    return {
        "name": name, "dialect": dialect, "tables": [t["name"] for t in tables], "catalog": "\n".join(lines),
        "roles": {k: v.get("table") for k, v in roles.items()}, "checks": checks, "evidence": evidence, "notes": notes,
        "services": roles.get("metrics", {}).get("services", []),
    }
