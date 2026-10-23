"""Discovery, config rendering and the setup flow, against a temporary SQLite system and a fake Prometheus."""

from __future__ import annotations

import sqlite3

import httpx
import yaml
from fastapi.testclient import TestClient

from overwatch.config import load_config
from overwatch.discovery import discover, render, write
from overwatch.discovery.prometheus import discover_prometheus
from overwatch.discovery.sql import discover_sql, sqlalchemy_url
from overwatch.server import create_app


def make_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE request_metrics (ts TIMESTAMP, service TEXT, p95_ms REAL, error_rate REAL);
        CREATE TABLE app_logs (ts TIMESTAMP, service TEXT, level TEXT, message TEXT);
        CREATE TABLE deploys (ts TIMESTAMP, service TEXT, version TEXT, description TEXT);
        CREATE TABLE customers (id INTEGER, name TEXT);
        INSERT INTO request_metrics VALUES (datetime('now'), 'orders', 12, 0.01), (datetime('now'), 'gateway', 90, 0.0);
    """)
    db.commit()
    db.close()
    return f"sqlite:///{path}"


def test_postgres_urls_get_the_bundled_driver():
    assert sqlalchemy_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert sqlalchemy_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert sqlalchemy_url("sqlite:///x.db") == "sqlite:///x.db"


def test_sql_discovery_recognises_metrics_logs_and_changes(tmp_path):
    s = discover_sql("ops", make_db(tmp_path / "ops.db"))
    assert s["roles"] == {"metrics": "request_metrics", "logs": "app_logs", "changes": "deploys"}
    assert sorted(s["services"]) == ["gateway", "orders"]
    names = {c["name"] for c in s["checks"]}
    assert {"orders_latency_ms", "orders_error_rate", "gateway_latency_ms", "gateway_error_rate"} <= names
    latency = next(c for c in s["checks"] if c["name"] == "orders_latency_ms")
    assert latency["relative_factor"] == 4 and "threshold" not in latency  # nothing to guess
    assert {e["name"] for e in s["evidence"]} >= {"ops_changes", "ops_top_errors"}
    assert "customers(id integer, name text)" in s["catalog"]


def test_prometheus_discovery_builds_red_checks_from_standard_metrics():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/label/__name__/values":
            data = ["http_requests_total", "http_request_duration_seconds_bucket", "up", "go_goroutines"]
        elif path == "/api/v1/series":
            data = [{"__name__": "http_requests_total", "service": "orders", "status": "200"},
                    {"__name__": "http_requests_total", "service": "orders", "status": "500"}]
        elif path == "/api/v1/label/job/values":
            data = ["orders"]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"status": "success", "data": data})

    p = discover_prometheus("http://prom:9090", transport=httpx.MockTransport(handler))
    checks = {c["name"]: c for c in p["checks"]}
    assert set(checks) == {"orders_p95_latency_ms", "orders_error_rate", "orders_up"}
    assert 'status=~"5.."' in checks["orders_error_rate"]["query"]
    assert checks["orders_up"]["kind"] == "heartbeat"
    assert "go_goroutines" not in p["catalog"]  # runtime noise is left out


def test_rendered_config_loads_and_keeps_secrets_out_of_the_yaml(tmp_path, monkeypatch):
    url = make_db(tmp_path / "ops.db")
    plan = discover(None, "", {"ops": url})
    plan["checks"][0]["enabled"] = False
    text, env = render(plan, {"api_key": "nvapi-secret", "base_url": "https://ai.example/v1"})
    assert "nvapi-secret" not in text and url not in text
    assert env["LLM_API_KEY"] == "nvapi-secret" and env["OW_OPS_URL"] == url
    assert plan["checks"][0]["name"] not in yaml.safe_load(text)["checks"].__repr__()

    result = write(plan, tmp_path / "overwatch.yaml", {"api_key": "nvapi-secret"})
    assert result["backup"] is None and (tmp_path / ".env").stat().st_mode & 0o077 == 0
    write(plan, tmp_path / "overwatch.yaml")  # second write keeps a backup
    assert len(list(tmp_path.glob("overwatch.yaml.bak-*"))) == 1
    monkeypatch.delenv("OW_OPS_URL", raising=False)
    cfg = load_config(tmp_path / "overwatch.yaml")  # reads .env next to it
    assert cfg.sources["ops"]["url"] == url and cfg.checks


def test_setup_flow_starts_watching_without_a_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OVERWATCH_TOKEN", raising=False)
    url = make_db(tmp_path / "ops.db")
    app = create_app(None, watch=False, config_path=tmp_path / "overwatch.yaml")
    with TestClient(app) as c:
        assert c.get("/api/status").status_code == 409  # not configured yet
        assert c.get("/api/setup/state").json()["configured"] is False
        found = c.post("/api/setup/discover", json={"docker": {"enabled": False}, "databases": [{"name": "ops", "url": url}]}).json()
        assert found["plan"]["checks"] and "checks:" in found["yaml"]
        r = c.post("/api/setup/apply", json={"plan": found["plan"], "ai": {"base_url": "http://ai.invalid/v1"}})
        assert r.status_code == 200, r.text
        assert c.get("/api/status").status_code == 200
        assert c.get("/api/setup/state").json()["configured"] is True
    app.state.runtime.ow.stop()


def test_external_hosts_come_from_settings_without_leaking_their_values():
    from overwatch.discovery.externals import hosts_in_env, merge_suggestions

    env = [
        "PAYMENT_API_URL=https://api.stripe.com/v1",
        "DATABASE_URL=postgresql://app:sup3rsecret@db.prod.example.com:5432/app",
        "SMTP_HOST=smtp.sendgrid.net",
        "CACHE_URL=redis://cache:6379",               # another container: not external
        "INTERNAL_URL=http://orders.svc.cluster.local",  # cluster-internal
        "PATH=/usr/bin", "PYTHON_VERSION=3.12.3",
    ]
    hits = hosts_in_env(env)
    assert hits == {"api.stripe.com": "PAYMENT_API_URL", "db.prod.example.com": "DATABASE_URL",
                    "smtp.sendgrid.net": "SMTP_HOST", "cache": "CACHE_URL", "orders.svc.cluster.local": "INTERNAL_URL"}
    suggestions = merge_suggestions({"payments": hits}, {}, internal={"orders", "cache"})
    names = [s["name"] for s in suggestions]
    assert names == ["api.stripe.com", "db.prod.example.com", "smtp.sendgrid.net"]
    assert "sup3rsecret" not in repr(suggestions)


def test_external_hosts_from_error_logs_are_counted_per_caller():
    from overwatch.discovery.externals import hosts_in_text, merge_suggestions

    lines = ["POST https://psp.example.com/v1/charge returned 429", "GET https://psp.example.com/v1/x timed out"]
    assert hosts_in_text(lines) == {"psp.example.com": 2}
    [s] = merge_suggestions({}, {"payments": hosts_in_text(lines)}, internal=set())
    assert s["callers"] == ["payments"] and "2 recent error log lines from payments" in s["evidence"][0]


def test_accepted_externals_become_topology_knowledge_and_checks():
    plan = {
        "system": {"name": "s", "description": ""}, "services": {"payments": {"what": "charges cards"}},
        "topology": {"payments": ["postgres"]}, "sources": {}, "catalog": "", "checks": [], "evidence": [],
        "log_source": {"type": "sql", "source": "ops", "table": "app_logs", "ts": "ts", "level": "level",
                       "dialect": "postgresql"},
        "externals": [
            {"name": "api.stripe.com", "callers": ["payments"], "health_url": "https://status.stripe.com/x",
             "log_text": "api.stripe.com", "enabled": True},
            {"name": "ignored.example.com", "callers": ["payments"], "enabled": False},
        ],
    }
    doc = yaml.safe_load(render(plan)[0])
    assert doc["topology"]["payments"] == ["postgres", "api.stripe.com"]
    assert doc["services"]["api.stripe.com"]["called_by"] == "payments"
    checks = {c["name"]: c for c in doc["checks"]}
    assert checks["api_stripe_com_up"]["kind"] == "probe"
    assert "LIKE '%api.stripe.com%'" in checks["api_stripe_com_errors"]["query"]
    assert "ignored.example.com" not in repr(doc)

    plan["log_source"] = {"type": "docker", "source": "docker"}
    doc = yaml.safe_load(render(plan)[0])
    assert next(c for c in doc["checks"] if c["name"] == "api_stripe_com_errors")["query"] == "count payments 2 api.stripe.com"
