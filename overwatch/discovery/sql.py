"""Discover what a SQL database can tell Overwatch: tables, change logs, logs, metrics, DB health views."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import create_engine, inspect, text

from .externals import hosts_in_text

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


def ago(target: str, n: int, unit: str) -> str:
    """'n units ago' in the SQL dialect the generated queries will run in."""
    if target == "zetaris":  # Spark SQL
        return f"current_timestamp() - INTERVAL {n} {unit.upper()}"
    return f"now() - interval '{n} {unit}'"


def write_warning(name: str, account: dict[str, Any] | None) -> str | None:
    """A note when the database account Overwatch connects with can change data (None when it is read-only)."""
    if not account:
        return None
    why = []
    if account["superuser"]:
        why.append("is a superuser")
    if account["writable_tables"]:
        n = account["writable_tables"]
        why.append(f"can write to {n} table{'s' if n != 1 else ''}")
    if account["can_create"] and not account["superuser"]:
        why.append("can create objects in the public schema")
    if not why:
        return None
    return (f"{name}: the account Overwatch connects with ('{account['user']}') {' and '.join(why)}. Overwatch only ever runs "
            "SELECT queries (checked in code and run in a read-only transaction), but give it a read-only account so a mistake "
            f"cannot become damage: CREATE ROLE overwatch LOGIN PASSWORD '...'; GRANT CONNECT ON DATABASE {account['database']} "
            "TO overwatch; GRANT USAGE ON SCHEMA public TO overwatch; GRANT SELECT ON ALL TABLES IN SCHEMA public TO overwatch; "
            "GRANT pg_read_all_stats TO overwatch; then use that account in the connection string.")


NUMERIC = ("int", "numeric", "decimal", "double", "float", "real")
NEVER_NEGATIVE = re.compile(r"(^|_)(count|amount|quantity|qty|total|price|balance|clicks?)(_|$)")
CREATED = re.compile(r"^(created|created_at|created_on)$")
LATER = re.compile(r"^(expires_at|expired_at|ended_at|finished_at|completed_at|closed_at|resolved_at|updated_at|deleted_at)$")
QUALITY_EVERY_SECONDS = 300
MAX_QUALITY_RULES = 6
WINDOW_ABOVE_ROWS = 5_000_000  # tables bigger than this are checked on recent rows only, so a rule stays cheap


def quality_rules(name: str, tables: list[dict[str, Any]], rows_estimate: dict[str, int], qual, since, source: str) -> list[dict[str, Any]]:
    """Data quality rules found from column names and types, as checks that count rows breaking a rule (threshold 0).
    Two kinds: a column that should never be negative (a count, amount, quantity, price or balance), and a later
    timestamp that comes before its row's creation time. Recent rows only when the table says when it was created,
    so a rule stays cheap on a big table. They run in the background (every_seconds), never in the 5-second pass."""
    rules: list[dict[str, Any]] = []
    for t in tables:
        if t["name"].startswith("pg_") or len(rules) >= MAX_QUALITY_RULES:
            continue
        cols = {c["name"]: str(c["type"]).lower() for c in t["columns"]}
        created = next((c for c in cols if CREATED.match(c) and any(x in cols[c] for x in TS_TYPES)), None)
        big = rows_estimate.get(t["name"], 0) > WINDOW_ABOVE_ROWS
        recent = f" AND {created} > {since(1, 'day')}" if created and big else ""
        small = created is not None or rows_estimate.get(t["name"], 0) <= 200_000  # no creation time: only a small table is scanned whole

        def rule(slug: str, breaks: str, text: str) -> None:
            # count the breaking rows with CASE inside the aggregate: Zetaris 2.4.3 returns every row for a WHERE that
            # compares two columns (WHERE a < b), so the comparison must not be in a WHERE
            where = f" WHERE {recent[5:]}" if recent else ""
            rules.append({
                "name": f"dq_{t['name']}_{slug}", "service": name, "source": source,
                "query": f"SELECT coalesce(sum(CASE WHEN {breaks} THEN 1 ELSE 0 END), 0) AS value FROM {qual(t['name'])}{where}",
                "threshold": 0, "comparison": ">", "every_seconds": QUALITY_EVERY_SECONDS, "severity": "medium",
                "description": f"data quality: {text}" + (" (rows from the last day)" if recent else ""),
                "why": f"data quality rule on {t['name']}: alerts when any row breaks it; checked every 5 minutes in the background",
            })

        if small:
            for c, typ in cols.items():
                if NEVER_NEGATIVE.search(c) and any(x in typ for x in NUMERIC):
                    rule(f"{c}_negative", f"{c} < 0", f"{t['name']}.{c} is negative")
        if created:
            for c, typ in cols.items():
                if LATER.match(c) and any(x in typ for x in TS_TYPES):
                    rule(f"{c}_before_created", f"{c} IS NOT NULL AND {c} < {created}", f"{t['name']}.{c} is earlier than {created}")
    return rules[:MAX_QUALITY_RULES]


def discover_sql(name: str, url: str, max_tables: int = 40, target: str = "direct") -> dict[str, Any]:
    """Inspect a database. `target` is where the generated checks and evidence will run: "direct" (its own
    dialect, plain table names) or "zetaris" (Spark SQL, tables qualified as <name>.<table>)."""
    zet = target == "zetaris"
    qual = (lambda table: f"{name}.{table}") if zet else (lambda table: table)
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
            # pg_stat_statements: "ready", "not_created" (extension not created in this database) or "not_loaded"
            # (the server was not started with it in shared_preload_libraries; querying it then raises an error)
            try:  # ask the view itself: reading the server setting needs a privilege the user may not have
                conn.execute(text("SELECT 1 FROM pg_stat_statements LIMIT 1"))
                state = "ready"
            except Exception as e:
                conn.rollback()
                state = "not_loaded" if "shared_preload_libraries" in str(e) else "not_created"
                if state == "not_created":  # a missing view looks the same either way; the setting says which, if readable
                    try:
                        loaded = "pg_stat_statements" in str(conn.execute(text("SHOW shared_preload_libraries")).scalar() or "")
                        state = "not_created" if loaded else "not_loaded"
                    except Exception:
                        conn.rollback()
                        state = "unknown"
            pg_health["pg_stat_statements"] = state == "ready"
            pg_health["statements_state"] = state
            try:
                conn.execute(text("SELECT count(*) FROM pg_stat_activity"))
                pg_health["pg_stat_activity"] = True
            except Exception:
                pg_health["pg_stat_activity"] = False
                conn.rollback()
            try:  # what could this account change? Overwatch only reads, so anything it can write is needless exposure
                row = conn.execute(text(
                    "SELECT current_user, current_database(), (SELECT rolsuper FROM pg_roles WHERE rolname = current_user), "
                    "(SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                    " WHERE c.relkind IN ('r', 'p') AND n.nspname = 'public' "
                    " AND has_table_privilege(current_user, c.oid, 'INSERT,UPDATE,DELETE,TRUNCATE')), "
                    "has_schema_privilege(current_user, 'public', 'CREATE')")).first()
                pg_health["account"] = {"user": row[0], "database": row[1], "superuser": bool(row[2]),
                                        "writable_tables": int(row[3]), "can_create": bool(row[4])}
            except Exception:
                conn.rollback()
            try:  # other users' sessions show state and query only to a superuser or a pg_read_all_stats member
                pg_health["activity_details"] = bool(conn.execute(text(
                    "SELECT rolsuper OR pg_has_role(current_user, 'pg_read_all_stats', 'member') "
                    "FROM pg_roles WHERE rolname = current_user")).scalar())
            except Exception:
                pg_health["activity_details"] = False
                conn.rollback()

        roles: dict[str, dict[str, Any]] = {}
        for t in tables:
            names = {c["name"] for c in t["columns"]}
            ts = _ts_column(t["columns"])
            if not ts:
                continue
            if "service" in names and ({"version"} & names or {"description"} & names) and "message" not in names:
                roles.setdefault("changes", {"table": t["name"], "ts": ts})
            elif {"level", "message"} <= names or ({"severity", "message"} <= names):
                roles.setdefault("logs", {"table": t["name"], "ts": ts, "service": "service" if "service" in names else None,
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

        log_hits: dict[str, dict[str, int]] = {}
        if lg := roles.get("logs"):  # which hosts do recent errors name? (external dependency clues)
            svc_col = lg["service"] or "''"
            recent = "now() - interval '1 hour'" if dialect == "postgresql" else "datetime('now', '-1 hour')"
            try:
                rows = conn.execute(text(
                    f"SELECT {svc_col}, message FROM {lg['table']} WHERE upper({lg['level']}) IN ('ERROR', 'FATAL', "
                    f"'CRITICAL') AND {lg['ts']} > {recent} LIMIT 2000")).fetchall()
            except Exception:
                rows = []
            for service, message in rows:
                for h, n in hosts_in_text([str(message)]).items():
                    log_hits.setdefault(service or "", {})[h] = log_hits.get(service or "", {}).get(h, 0) + n
    engine.dispose()

    # Catalog text for the Investigator.
    lines = [f"{name} ({'PostgreSQL' if dialect == 'postgresql' else dialect} SQL):"] if not zet else \
        [f"{name} ({'PostgreSQL' if dialect == 'postgresql' else dialect} database, in Zetaris as {name}.<table>):"]
    for t in tables:
        cols = ", ".join(f"{c['name']} {c['type'].split('(')[0]}" for c in t["columns"][:20])
        est = f"  (~{rows_estimate[t['name']]:,} rows)" if rows_estimate.get(t["name"], 0) > 0 else ""
        lines.append(f"  {qual(t['name'])}({cols}){est}")
    if pg_health.get("pg_stat_activity") and not zet:
        lines.append("  Postgres health views: pg_stat_activity, pg_locks, pg_indexes, pg_stat_user_tables"
                     + (", pg_stat_statements" if pg_health.get("pg_stat_statements") else ""))

    checks, evidence = [], []
    pg = dialect == "postgresql"
    if zet:
        window = ago("zetaris", 40, "seconds")
    else:
        window = "now() - interval '40 seconds'" if pg else "datetime('now', '-40 seconds')"

    def since(n: int, unit: str) -> str:
        if zet:
            return ago("zetaris", n, unit)
        return f"now() - interval '{n} {unit}'" if pg else f"datetime('now', '-{n} {unit}')"

    if m := roles.get("metrics"):
        for svc in m["services"]:
            key = svc.replace("-", "_").replace(".", "_")
            if m["latency"]:
                unit = "_ms" if m["latency"].endswith("_ms") else ""
                checks.append({
                    "name": f"{key}_latency{unit}", "service": svc, "source": "zetaris" if zet else name,
                    "query": f"SELECT avg({m['latency']}) AS value FROM {qual(m['table'])} "
                             f"WHERE service = '{svc}' AND {m['ts']} > {window}",
                    "relative_factor": 4, "relative_min": 100 if unit else 0.1, "no_data": "alert",
                    "description": f"{svc} {m['latency']}, last 40s", "severity": "high",
                    "why": f"{m['table']}.{m['latency']}: alerts at 4x its own normal, no threshold to guess",
                })
            if m["errors"]:
                checks.append({
                    "name": f"{key}_error_rate", "service": svc, "source": "zetaris" if zet else name,
                    "query": f"SELECT avg({m['errors']}) AS value FROM {qual(m['table'])} "
                             f"WHERE service = '{svc}' AND {m['ts']} > {window}",
                    "threshold": 0.05, "description": f"{svc} {m['errors']} (0-1), last 40s", "severity": "high",
                    "why": f"{m['table']}.{m['errors']}: alerts above 5% errors",
                })
        lat, err, t = m["latency"] or "NULL", m["errors"] or "NULL", qual(m["table"])
        if zet:
            q = (f"SELECT service, round(avg(CASE WHEN {m['ts']} > {since(2, 'minutes')} THEN {lat} END), 1) AS latency_now, "
                 f"round(avg({lat}), 1) AS latency_hour, "
                 f"round(avg(CASE WHEN {m['ts']} > {since(2, 'minutes')} THEN {err} END), 3) AS errors_now "
                 f"FROM {t} WHERE {m['ts']} > {since(1, 'hour')} GROUP BY service ORDER BY service")
        elif pg:
            q = (f"SELECT service, round((avg({lat}) FILTER (WHERE {m['ts']} > now() - interval '2 minutes'))::numeric, 1) AS latency_now, "
                 f"round(avg({lat})::numeric, 1) AS latency_hour, "
                 f"round((avg({err}) FILTER (WHERE {m['ts']} > now() - interval '2 minutes'))::numeric, 3) AS errors_now "
                 f"FROM {t} WHERE {m['ts']} > now() - interval '1 hour' GROUP BY 1 ORDER BY 1")
        else:
            q = f"SELECT service, avg({lat}), avg({err}) FROM {t} GROUP BY 1"
        evidence.append({"name": f"{name}_service_health",
                         "label": "Latency and errors per service, last 2 minutes vs last hour",
                         "source": "zetaris" if zet else name, "query": q})
    if c := roles.get("changes"):
        evidence.append({
            "name": f"{name}_changes", "label": f"Changes in the last 30 minutes ({c['table']})",
            "suspects": True, "source": "zetaris" if zet else name,
            "query": f"SELECT * FROM {qual(c['table'])} WHERE {c['ts']} > {since(30, 'minutes')} "
                     f"ORDER BY {c['ts']} DESC LIMIT 15",
        })
    if lg := roles.get("logs"):
        msg = "substr(message, 1, 160)" if zet else "left(message, 160)"
        evidence.append({
            "name": f"{name}_top_errors", "label": f"Top error messages in the last 5 minutes ({lg['table']})",
            "source": "zetaris" if zet else name,
            "query": f"SELECT {msg} AS message, count(*) AS n FROM {qual(lg['table'])} "
                     f"WHERE upper({lg['level']}) IN ('ERROR', 'FATAL', 'CRITICAL') AND {lg['ts']} > {since(5, 'minutes')} "
                     "GROUP BY 1 ORDER BY n DESC LIMIT 10" if (pg or zet) else
                     f"SELECT message, count(*) AS n FROM {lg['table']} GROUP BY 1 ORDER BY n DESC LIMIT 10",
        })
    if pg_health.get("pg_stat_activity") and not zet:  # system views are not federated tables
        evidence.append({
            "name": f"{name}_db_activity", "label": f"Busy or waiting sessions on {name}", "source": name,
            "query": "SELECT application_name, state, wait_event_type, wait_event, left(query, 120) AS query, "
                     "clock_timestamp() - query_start AS running_for FROM pg_stat_activity "
                     "WHERE state <> 'idle' AND pid <> pg_backend_pid() ORDER BY query_start LIMIT 10",
        })

    checks += quality_rules(name, tables, rows_estimate, qual, since, "zetaris" if zet else name)

    notes = []
    if pg and (w := write_warning(name, pg_health.get("account"))):
        notes.append(w)
    if pg:
        if not pg_health.get("activity_details"):
            notes.append(f"{name}: Overwatch's database user cannot see other sessions' queries. Run "
                         "`GRANT pg_read_all_stats TO <that user>;` (or connect as a superuser). Until then the "
                         "slow-query check is off and the sessions evidence is incomplete.")
        state = pg_health.get("statements_state")
        if state == "not_loaded":
            notes.append(f"{name}: to let Overwatch see which queries are slow, start Postgres with "
                         "`-c shared_preload_libraries=pg_stat_statements` (restart needed), then run "
                         "`CREATE EXTENSION pg_stat_statements;` in this database.")
        elif state in ("not_created", "unknown"):
            notes.append(f"{name}: run `CREATE EXTENSION pg_stat_statements;` in this database so Overwatch can see slow queries."
                         + (" If that says the library is not loaded, first start Postgres with "
                            "`-c shared_preload_libraries=pg_stat_statements` and restart it." if state == "unknown" else ""))
    return {
        "name": name, "dialect": dialect, "tables": [t["name"] for t in tables], "catalog": "\n".join(lines),
        "roles": {k: v.get("table") for k, v in roles.items()}, "checks": checks, "evidence": evidence, "notes": notes,
        "services": roles.get("metrics", {}).get("services", []),
        "pg_activity": bool(pg and pg_health.get("pg_stat_activity") and pg_health.get("activity_details")),
        "logs": {**roles["logs"], "table": qual(roles["logs"]["table"])} if roles.get("logs") else None,
        "log_hits": log_hits,
    }
