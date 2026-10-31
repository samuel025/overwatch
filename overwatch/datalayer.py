"""The data layer: how the systems a user connects become one queryable thing.

Two backends:
  direct   every connected database is its own source (what runs today, and the fallback for development)
  zetaris  Overwatch registers each connected database as a Zetaris datasource and agents query only Zetaris,
           so one query can join tables from different systems (deploys in one database, orders in another)

The end user never configures Zetaris: they add their databases in the setup page; Overwatch registers them.
The Zetaris endpoint itself comes from the deployment's environment (ZETARIS_JDBC_URL ...).
"""

from __future__ import annotations

import os
import re
from typing import Any

from sqlalchemy.engine import make_url

from .sources.base import SourceError
from .sources.zetaris import ZetarisSource

JDBC_DRIVERS = {"postgresql": "org.postgresql.Driver", "mysql": "com.mysql.cj.jdbc.Driver",
                "mariadb": "org.mariadb.jdbc.Driver", "mssql": "com.microsoft.sqlserver.jdbc.SQLServerDriver"}


def zetaris_env() -> dict[str, str]:
    """The deployment's Zetaris connection, or {} when this install does not use Zetaris."""
    cfg = {"jdbc_url": os.environ.get("ZETARIS_JDBC_URL", ""), "driver_class": os.environ.get("ZETARIS_DRIVER_CLASS", ""),
           "driver_jar": os.environ.get("ZETARIS_DRIVER_JAR", ""), "username": os.environ.get("ZETARIS_USER", ""),
           "password": os.environ.get("ZETARIS_PASSWORD", "")}
    return cfg if cfg["jdbc_url"] else {}


def datasource_name(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_") or "source"


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_jdbc(sqlalchemy_url: str) -> dict[str, str]:
    """postgresql+psycopg://u:p@host:5432/db  ->  jdbc url, driver class, user, password."""
    url = make_url(sqlalchemy_url)
    backend = url.get_backend_name()
    if backend not in JDBC_DRIVERS:
        raise ValueError(f"cannot register a '{backend}' database with Zetaris yet (supported: {', '.join(JDBC_DRIVERS)})")
    port = f":{url.port}" if url.port else ""
    host = url.host
    if host in ("localhost", "127.0.0.1", "::1") and os.environ.get("ZETARIS_HOST_ALIAS"):
        host = os.environ["ZETARIS_HOST_ALIAS"]  # Zetaris runs in Docker: "localhost" there is not the user's machine
    return {"jdbcurl": f"jdbc:{backend}://{host}{port}/{url.database or ''}", "jdbcdriver": JDBC_DRIVERS[backend],
            "username": url.username or "", "password": url.password or ""}


def registration_statements(name: str, sqlalchemy_url: str, description: str = "") -> list[str]:
    """The Zetaris SQL that makes a user's database queryable as <name>.<table>."""
    ds = datasource_name(name)
    jdbc = to_jdbc(sqlalchemy_url)
    options = ", ".join(f"{k} {_q(v)}" for k, v in {**jdbc, "fetchsize": "1000"}.items())
    return [
        f"CREATE DATASOURCE {ds} DESCRIBE BY {_q(description or 'connected by Overwatch')} OPTIONS ({options})",
        f"REGISTER DATASOURCE TABLES FROM {ds}",
    ]


# Live Postgres internals are not tables, so registering a database never brings them in. A Zetaris datasource view runs
# its SELECT inside the source database, so these make sessions, locks and scan counts queryable as <db>.live_*,
# next to the tables, without adding anything to the user's schema.
LIVE_VIEWS = {
    "live_sessions": (
        "SELECT pid, usename, application_name, state, wait_event_type, wait_event, left(query, 200) AS query, "
        "round(cast(extract(epoch FROM clock_timestamp() - query_start) AS numeric), 1) AS running_seconds, "
        "round(cast(extract(epoch FROM clock_timestamp() - xact_start) AS numeric), 1) AS transaction_seconds, "
        "array_to_string(pg_blocking_pids(pid), ',') AS blocked_by FROM pg_stat_activity WHERE pid <> pg_backend_pid()"),
    "live_locks": (
        "SELECT l.pid, l.mode, l.granted, c.relname AS relation, array_to_string(pg_blocking_pids(l.pid), ',') AS blocked_by "
        "FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE c.relnamespace NOT IN (SELECT oid FROM pg_namespace WHERE nspname IN ('pg_catalog', 'information_schema'))"),
    "live_table_stats": (
        "SELECT relname, seq_scan, idx_scan, n_live_tup, n_dead_tup, last_autovacuum FROM pg_stat_user_tables"),
}

LIVE_CATALOG = (
    "{n}.live_sessions(pid, usename, application_name, state, wait_event_type, wait_event, query, running_seconds, "
    "transaction_seconds, blocked_by): who is connected and what they run or wait on right now (blocked_by lists the pids "
    "holding what they wait for); {n}.live_locks(pid, mode, granted, relation, blocked_by): current table locks, granted = "
    "false is a waiter; {n}.live_table_stats(relname, seq_scan, idx_scan, n_live_tup, n_dead_tup, last_autovacuum)")


def live_view_statements(name: str) -> list[str]:
    ds = datasource_name(name)
    return [f"CREATE DATASOURCE VIEW {v} FROM {ds} AS {sql}" for v, sql in LIVE_VIEWS.items()]


def _is_postgres(url: str) -> bool:
    return make_url(url).get_backend_name() == "postgresql"


REGISTER_SECONDS = 40  # per statement; registering reads the database's schema, so a hung database shows up here


def register(databases: dict[str, str], zetaris: dict[str, Any] | None = None) -> list[str]:
    """Register every database with the deployment's Zetaris. Returns what was run."""
    cfg = zetaris or zetaris_env()
    if not cfg:
        raise RuntimeError("Zetaris is not configured for this deployment (ZETARIS_JDBC_URL is not set)")
    src = ZetarisSource("zetaris", cfg)
    try:
        done: list[str] = []
        failed: list[str] = []
        for name, url in databases.items():
            try:  # a database that is down or paused must not keep the others (or startup) waiting for ever
                done += src.admin(registration_statements(name, url), timeout=REGISTER_SECONDS)
                if name != "overwatch" and _is_postgres(url):  # a datasource's views go with it, so they follow every registration
                    done += src.admin(live_view_statements(name), timeout=REGISTER_SECONDS)
            except SourceError as e:
                failed.append(f"{name}: {e}")
        if failed:
            raise RuntimeError("; ".join(failed))
        return done
    finally:
        src.close()


USL_CONTAINER = "usl"
USL_VIEWS = {  # business-named views over Overwatch's own history (registered as the `overwatch` datasource)
    "check_health": (
        "SELECT check_name, service, count(*) AS samples, round(avg(value), 2) AS avg_value, round(max(value), 2) AS max_value, "
        "round(100 * avg(failing), 1) AS pct_time_failing FROM overwatch.check_samples GROUP BY check_name, service"),
    "container_status": (
        "SELECT service, container, state, restarts, health, ts FROM (SELECT *, row_number() OVER (PARTITION BY container "
        "ORDER BY ts DESC) AS rn FROM overwatch.container_state) WHERE rn = 1"),
    "quality_results": (
        "SELECT check_name AS rule, value AS rows_breaking_rule, ts FROM (SELECT *, row_number() OVER (PARTITION BY check_name "
        "ORDER BY ts DESC) AS rn FROM overwatch.check_samples WHERE substring(check_name, 1, 3) = 'dq_') WHERE rn = 1"),
    "incident_history": (  # every incident Overwatch has handled, flattened from the stored record
        "SELECT id, opened_at, status, get_json_object(data, '$.severity') AS severity, "
        "get_json_object(data, '$.origin_service') AS origin_service, get_json_object(data, '$.root_cause') AS root_cause, "
        "cast(get_json_object(data, '$.confidence') AS double) AS confidence, "
        "get_json_object(data, '$.recovered_at') AS recovered_at, "
        "cast(get_json_object(data, '$.grounding.verified') AS int) AS quotes_verified, "
        "cast(get_json_object(data, '$.grounding.total') AS int) AS quotes_total FROM overwatch.incidents"),
    "change_impact": (  # each release or config change, and what its service's checks did in the 15 minutes after
        "SELECT c.ts AS change_ts, c.service, c.version, c.kind, c.description, count(s.ts) AS samples_after, "
        "sum(CASE WHEN s.failing = 1 THEN 1 ELSE 0 END) AS failing_samples_after, "
        "count(DISTINCT CASE WHEN s.failing = 1 THEN s.check_name END) AS checks_failing_after "
        "FROM overwatch.changes c LEFT JOIN overwatch.check_samples s ON s.service = c.service "
        "AND to_timestamp(s.ts) >= to_timestamp(c.ts) AND to_timestamp(s.ts) <= to_timestamp(c.ts) + INTERVAL 15 MINUTES "
        "GROUP BY c.ts, c.service, c.version, c.kind, c.description"),
}


# A Zetaris Unified Semantic Layer proper (Data Product > Unified Semantic Layer in its UI): a namespace holding a named USL
# whose tables have keys and descriptions. Compiled once from DDL, then each table is activated with the SELECT that
# feeds it, so the tables stay live over Overwatch's own history. (The SchemaStore container `usl` below is the query
# helper layer; this is the governed model of the same data.)
USL_NAMESPACE = "lightning.metastore.overwatch"
USL_NAME = "incident_response"


def usl_object_statements() -> list[str]:
    ns, usl = USL_NAMESPACE, f"{USL_NAMESPACE}.{USL_NAME}"
    return [
        "CREATE NAMESPACE IF NOT EXISTS lightning.metastore",
        f"CREATE NAMESPACE IF NOT EXISTS {ns}",
        f"""COMPILE USL IF NOT EXISTS {USL_NAME} DEPLOY NAMESPACE {ns} DDL
CREATE TABLE service (
    name varchar(200) NOT NULL PRIMARY KEY
);

CREATE TABLE incident (
    id varchar(20) NOT NULL PRIMARY KEY,
    opened_at varchar(40),
    status varchar(40),
    severity varchar(20),
    origin_service varchar(200),
    root_cause varchar(2000),
    confidence double
);

CREATE TABLE change_event (
    change_id varchar(400) NOT NULL PRIMARY KEY,
    ts varchar(40),
    service varchar(200) NOT NULL FOREIGN KEY REFERENCES service(name),
    version varchar(200),
    kind varchar(40),
    description varchar(2000)
);""",
        f"UPDATE USL {usl} SET DESCRIPTION 'What Overwatch knows about incidents: the services it watches, every incident it "
        "handled, and every release or config change it saw.'",
        f"UPDATE USL {usl}.service SET TABLE DESCRIPTION 'The services Overwatch has check results for.'",
        f"UPDATE USL {usl}.incident SET TABLE DESCRIPTION 'One row per incident Overwatch investigated: cause, severity and confidence.'",
        f"UPDATE USL {usl}.change_event SET TABLE DESCRIPTION 'One row per release or config change, reported to or learned by Overwatch.'",
        # coalesce: a column fed by DISTINCT or an expression is nullable to Zetaris, and the key columns are NOT NULL
        f"ACTIVATE USL TABLE {usl}.service AS SELECT DISTINCT coalesce(service, '') AS name FROM overwatch.check_samples "
        "WHERE service IS NOT NULL AND service <> ''",
        f"ACTIVATE USL TABLE {usl}.incident AS SELECT id, opened_at, status, get_json_object(data, '$.severity') AS severity, "
        "get_json_object(data, '$.origin_service') AS origin_service, get_json_object(data, '$.root_cause') AS root_cause, "
        "cast(get_json_object(data, '$.confidence') AS double) AS confidence FROM overwatch.incidents",
        f"ACTIVATE USL TABLE {usl}.change_event AS SELECT concat(ts, '|', service, '|', coalesce(version, '')) AS change_id, "
        "ts, service, version, kind, description FROM overwatch.changes",
    ]


def usl_statements(prometheus_up_view: str = "") -> list[tuple[str, str]]:
    """The DDL of Overwatch's Unified Semantic Layer: a SchemaStore container `usl` with business-named views. Five read
    Overwatch's own history; `target_status` joins Prometheus (live, through promql) with that history in one statement."""
    out = [(USL_CONTAINER, f"CREATE SCHEMASTORE CONTAINER {USL_CONTAINER}")]
    views = dict(USL_VIEWS)
    if prometheus_up_view:
        views["target_status"] = (
            "SELECT t.service, t.job, t.up, c.state AS container_state, c.restarts FROM (SELECT "
            "substring_index(metric['instance'], ':', 1) AS service, metric['job'] AS job, value AS up "
            f"FROM ow_views.{prometheus_up_view}) t LEFT JOIN (SELECT service, state, restarts FROM (SELECT *, "
            "row_number() OVER (PARTITION BY container ORDER BY ts DESC) AS rn FROM overwatch.container_state) WHERE rn = 1) c "
            "ON c.service = t.service")
    for view, select in views.items():
        out.append((view, f"CREATE OR REPLACE SCHEMASTORE VIEW {view} WITH CONTAINER {USL_CONTAINER} AS {select}"))
    return out


def create_usl(src: ZetarisSource) -> list[str]:
    """Create (or refresh) the USL. Safe to repeat. Returns the names created."""
    up_view = ""
    if src.rest.get("prometheus"):
        try:
            up_view = src._rest_view("promql", "up") or ""
        except SourceError:
            up_view = ""  # Prometheus is down right now: the other views still work
    done = []
    for name, sql in usl_statements(up_view):
        src._ensure_once(sql) if name == USL_CONTAINER else src._plain(sql)
        done.append(f"{USL_CONTAINER}.{name}" if name != USL_CONTAINER else name)
    try:  # the governed model is an extra: if it fails, the views above still work
        src.admin(usl_object_statements(), timeout=60)
        done.append(f"{USL_NAMESPACE}.{USL_NAME}")
    except SourceError:
        pass
    return done


def source_config() -> dict[str, str]:
    """The `zetaris` source block for overwatch.yaml: references only, secrets stay in .env."""
    return {"type": "zetaris", "jdbc_url": "${ZETARIS_JDBC_URL}", "driver_class": "${ZETARIS_DRIVER_CLASS}",
            "driver_jar": "${ZETARIS_DRIVER_JAR}", "username": "${ZETARIS_USER}", "password": "${ZETARIS_PASSWORD}"}


__all__ = ["usl_object_statements", "USL_NAMESPACE", "USL_NAME", "live_view_statements", "LIVE_CATALOG", "register", "create_usl", "usl_statements", "registration_statements", "to_jdbc", "zetaris_env", "source_config", "datasource_name"]
