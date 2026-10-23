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
