"""The Zetaris data layer: registration SQL, the JDBC source, and discovery that targets it. No Zetaris needed."""

import sys
import types

import pytest

from overwatch import datalayer
from overwatch.discovery import discover, render
from overwatch.discovery.sql import discover_sql
from overwatch.sources.base import SourceError
from overwatch.sources.zetaris import ZetarisSource
from tests.test_discovery import make_db


def test_registration_statements_turn_a_postgres_url_into_zetaris_sql():
    create, register = datalayer.registration_statements("Shop DB", "postgresql+psycopg://reader:p%22w@db.internal:5433/shop")
    assert create.startswith("CREATE DATASOURCE shop_db DESCRIBE BY")
    assert 'jdbcurl "jdbc:postgresql://db.internal:5433/shop"' in create
    assert 'jdbcdriver "org.postgresql.Driver"' in create and 'username "reader"' in create
    assert 'password "p\\"w"' in create  # quotes in a password cannot break out of the statement
    assert register == "REGISTER DATASOURCE TABLES FROM shop_db"
    with pytest.raises(ValueError, match="cannot register"):
        datalayer.to_jdbc("sqlite:///x.db")


def test_discovery_for_zetaris_writes_spark_sql_over_qualified_tables(tmp_path):
    s = discover_sql("ops", make_db(tmp_path / "ops.db"), target="zetaris")
    latency = next(c for c in s["checks"] if c["name"] == "orders_latency_ms")
    assert latency["source"] == "zetaris" and "FROM ops.request_metrics" in latency["query"]
    assert "current_timestamp() - INTERVAL 40 SECONDS" in latency["query"] and "now()" not in latency["query"]
    texts = {e["name"]: e["query"] for e in s["evidence"]}
    assert "FROM ops.deploys" in texts["ops_changes"] and "substr(message" in texts["ops_top_errors"]
    assert "FILTER" not in texts["ops_service_health"] and "::" not in texts["ops_service_health"]
    assert "ops.app_logs(" in s["catalog"] and s["logs"]["table"] == "ops.app_logs"


def test_zetaris_plan_registers_databases_and_renders_one_source(tmp_path):
    plan = discover(None, "", {"ops": make_db(tmp_path / "ops.db")}, zetaris=True)
    assert plan["databases"] == {"ops": f"sqlite:///{tmp_path / 'ops.db'}"}
    assert list(plan["sources"]) == ["zetaris"] and plan["log_source"]["dialect"] == "spark"
    text, env = render(plan)
    assert "${ZETARIS_JDBC_URL}" in text and "ops.db" not in text  # no user database address in the config
    assert env["OW_OPS_URL"].endswith("ops.db")  # kept for re-registration, in .env


def test_zetaris_source_runs_read_only_queries_and_registration(monkeypatch):
    ran = []

    class Cursor:
        description = [("n",)]

        def execute(self, sql):
            ran.append(sql)

        def fetchmany(self, n):
            return [(1,)]

        def fetchall(self):
            return [(1,)]

        def close(self):
            pass

    fake = types.SimpleNamespace(connect=lambda *a, **k: types.SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None))
    monkeypatch.setitem(sys.modules, "jaydebeapi", fake)
    src = ZetarisSource("zetaris", {"jdbc_url": "jdbc:x", "driver_class": "com.Z"})
    assert src.query("SELECT 1 AS n") == [{"n": 1}]
    assert src.query("SHOW DATASOURCES") == [{"n": 1}]
    for bad in ("DROP TABLE shop.orders", "CREATE DATASOURCE x OPTIONS ()", "SELECT 1; SELECT 2", "SHOW DATASOURCES; DROP TABLE a"):
        with pytest.raises(SourceError):
            src.query(bad)
    src.admin(["CREATE DATASOURCE a OPTIONS ()"])
    assert ran[-1].startswith("CREATE DATASOURCE")  # only the admin path can run DDL


def test_zetaris_source_needs_its_connection_settings():
    with pytest.raises(SourceError, match="jdbc_url"):
        ZetarisSource("zetaris", {})


def _fake_zetaris(monkeypatch, plain, prepared, shape=("success", "vector", 1)):
    """A fake Zetaris connection that records plain statements (jconn) separately from prepared ones (cursor)."""
    class Cursor:
        description = [("value",)]
        rows = [(1.0,)]

        def execute(self, sql):
            prepared.append(sql)
            probe = "resultType" in sql  # the question "what shape is the answer?"
            self.description = [("status",), ("kind",), ("n",)] if probe else [("value",)]
            self.rows = [shape] if probe else [(1.0,)]

        def fetchmany(self, n):
            return self.rows

        def fetchall(self):
            return self.rows

        def close(self):
            pass

    class Statement:
        def execute(self, sql):
            plain.append(sql)

        def close(self):
            pass

    conn = types.SimpleNamespace(cursor=lambda: Cursor(), close=lambda: None,
                                 jconn=types.SimpleNamespace(createStatement=lambda: Statement()))
    monkeypatch.setitem(sys.modules, "jaydebeapi", types.SimpleNamespace(connect=lambda *a, **k: conn))


def test_promql_runs_through_zetaris_as_a_rest_table_with_the_expression_encoded(monkeypatch):
    plain, prepared = [], []
    _fake_zetaris(monkeypatch, plain, prepared)
    src = ZetarisSource("zetaris", {"jdbc_url": "jdbc:x", "driver_class": "com.Z", "rest": {"prometheus": "http://prom:9090"}})
    assert "promql" in src.language
    expr = 'sum(rate(http_requests_total{job="api"}[2m])) by (route)'
    assert src.query(f"promql {expr}") == [{"value": 1.0}]
    table = next(s for s in plain if s.startswith("CREATE LIGHTNING REST TABLE"))
    assert "http://prom:9090/api/v1/query?query=sum%28rate%28http_requests_total%7Bjob%3D%22api%22%7D%5B2m%5D%29%29" in table
    assert '"' not in table.split('endpoint "', 1)[1].split('"', 1)[0]  # nothing an agent sends can end the string early
    assert any(s.startswith("CREATE OR REPLACE SCHEMASTORE VIEW") and "explode(data.result)" in s for s in plain)
    assert not any("REST TABLE" in s for s in prepared)  # a '?' in the URL must not go through a prepared statement
    assert prepared[-1].startswith("SELECT * FROM ow_views.promql_")
    before = len(plain)
    src.query(f"promql {expr}")
    assert len(plain) == before  # the second ask reuses the table
    with pytest.raises(SourceError, match="not configured"):
        ZetarisSource("z", {"jdbc_url": "jdbc:x", "driver_class": "com.Z"}).query("logql {app=\"x\"}")


def test_promql_handles_empty_and_scalar_answers_and_rejections(monkeypatch):
    cfg = {"jdbc_url": "jdbc:x", "driver_class": "com.Z", "rest": {"prometheus": "http://prom:9090"}}
    plain, prepared = [], []
    _fake_zetaris(monkeypatch, plain, prepared, shape=("success", "vector", 0))
    assert ZetarisSource("z", cfg).query("promql up{job=\"none\"}") == []  # nothing to flatten: no view, table dropped
    assert not any("SCHEMASTORE VIEW" in s for s in plain) and any(s.startswith("DROP LIGHTNING DATABASE TABLE") for s in plain)
    plain, prepared = [], []
    _fake_zetaris(monkeypatch, plain, prepared, shape=("success", "scalar", 2))
    ZetarisSource("z", cfg).query("promql vector(0)")
    assert "data.result[1] AS value" in next(s for s in plain if "SCHEMASTORE VIEW" in s)
    _fake_zetaris(monkeypatch, [], [], shape=("error", None, None))
    with pytest.raises(SourceError, match="did not accept"):
        ZetarisSource("z", cfg).query("promql not a query")


def test_zetaris_reaches_localhost_urls_through_the_docker_host_alias(monkeypatch):
    from overwatch.sources.zetaris import reachable

    monkeypatch.setenv("ZETARIS_HOST_ALIAS", "172.17.0.1")
    assert reachable("http://localhost:9090") == "http://172.17.0.1:9090"
    assert reachable("http://prom.internal:9090/x?y=1") == "http://prom.internal:9090/x?y=1"
    monkeypatch.delenv("ZETARIS_HOST_ALIAS")
    assert reachable("http://localhost:9090") == "http://localhost:9090"


def test_zetaris_mode_discovery_hides_covered_sources_and_keeps_live_postgres(monkeypatch):
    monkeypatch.setenv("OVERWATCH_STATE_URL", "postgresql+psycopg://o:o@localhost:15433/overwatch")
    plan = discover(None, "", {}, zetaris=True)
    assert plan["sources"]["zetaris"]["type"] == "zetaris" and plan["state_url"] == "${OVERWATCH_STATE_URL}"
    monkeypatch.delenv("OVERWATCH_STATE_URL")
    assert any("OVERWATCH_STATE_URL" in e for e in discover(None, "", {}, zetaris=True)["errors"])


def test_internal_sources_are_not_offered_to_the_investigator():
    from overwatch.agents.investigator import Investigator
    from tests.test_core import FakeSource, ScriptedLLM, make_config

    cfg = make_config()
    cfg.sources = {"db": {}, "prom": {"type": "prometheus", "internal": True}}
    inv = Investigator(cfg, ScriptedLLM([]), {"db": FakeSource({}), "prom": FakeSource({})})
    assert "prom" in inv.hidden
    assert inv._run_query({"source": "prom", "query": "up"}).error.startswith("unknown source 'prom'")
    assert not inv._run_query({"source": "prom", "query": "up", "_pack": True}).error.startswith("unknown source")


def test_a_hung_data_source_cannot_hold_a_query_forever(monkeypatch):
    import threading
    import time
    import types

    release = threading.Event()

    class Cursor:
        description = None

        def execute(self, sql):
            release.wait(5)  # a paused database: connects, never answers

        def close(self):
            pass

    class Conn:
        def cursor(self):
            return Cursor()

    monkeypatch.setitem(sys.modules, "jaydebeapi", types.SimpleNamespace(connect=lambda *a, **k: Conn()))
    src = ZetarisSource("z", {"jdbc_url": "jdbc:x", "driver_class": "x", "timeout_seconds": 0.3})
    started = time.monotonic()
    with pytest.raises(SourceError, match="did not answer within"):
        src.query("SELECT 1")
    release.set()
    assert time.monotonic() - started < 2
    assert src._conn is None  # the next query opens a fresh connection


def test_postgres_gets_a_connection_saturation_check_on_its_own_service(monkeypatch):
    from overwatch import discovery

    def fake_sql(name, url, max_tables=40, target="direct"):
        return {"name": name, "dialect": "postgresql", "tables": [], "catalog": "", "checks": [], "notes": [],
                "evidence": [{"name": "a", "label": "l", "source": name, "query": "SELECT 1 FROM pg_stat_activity"}],
                "services": [], "logs": None, "log_hits": {}, "pg_activity": True}

    monkeypatch.setattr(discovery, "discover_sql", fake_sql)
    monkeypatch.setattr(discovery, "discover_docker", lambda *a, **k: {
        "project": "shop", "service_label": "x", "projects": ["shop"], "topology": {}, "checks": [], "runtime_facts": "",
        "notes": [], "env_hits": {}, "log_hits": {}, "internal": [],
        "services": {"postgres": {"databases": True, "ports": [{"PublicPort": 5433}]}, "api": {"ports": []}}})
    for zet in (True, False):
        plan = discovery.discover({"project": "shop"}, "", {"main": "postgresql://u:p@localhost:5433/db"}, zetaris=zet)
        check = next(c for c in plan["checks"] if c["name"] == "main_connections_used_pct")
        assert check["service"] == "postgres" and check["threshold"] == 85
        assert check["source"] == ("main_live" if zet else "main")
        assert "max_connections" in check["query"]
        slow = next(c for c in plan["checks"] if c["name"] == "main_longest_query_seconds")
        assert slow["service"] == "postgres" and slow["source"] == check["source"] and "pg_stat_activity" in slow["query"]


def test_write_warning_only_for_accounts_that_can_change_data():
    from overwatch.discovery.sql import write_warning

    base = {"user": "overwatch", "database": "shop", "superuser": False, "writable_tables": 0, "can_create": False}
    assert write_warning("db", base) is None                                    # a read-only account: nothing to say
    assert write_warning("db", None) is None                                    # could not tell: stay quiet
    sup = write_warning("db", {**base, "user": "postgres", "superuser": True})
    assert "superuser" in sup and "CREATE ROLE overwatch" in sup and "ON DATABASE shop" in sup
    rw = write_warning("db", {**base, "writable_tables": 3})
    assert "can write to 3 tables" in rw and "pg_read_all_stats" in rw
    assert "can write to 1 table." in write_warning("db", {**base, "writable_tables": 1})  # singular
    assert "create objects" in write_warning("db", {**base, "can_create": True})
