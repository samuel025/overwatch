"""Editing a running configuration from the console: settings.read() / settings.save()."""

import yaml

from overwatch import settings
from overwatch.config import load_config

CONFIG = """\
# my system
system: {name: shop, description: a shop}
docker: {project: shop}
services:
  api: {what: takes orders, normal: p95 50ms}
  psp.example.com: {what: external card processor, normal: 40 ms}
topology:
  api: [psp.example.com]
checks:
  - name: api_up
    kind: container
    service: api
  - name: card_errors
    service: psp.example.com
    source: docker
    query: count api 2 psp.example.com
    threshold: 5
llm: {base_url: "${LLM_BASE_URL:-http://x/v1}", api_key: "${LLM_API_KEY}", investigator_model: m}
notifiers: [{type: console}]
"""


def test_round_trip_keeps_hand_written_checks_and_moves_secrets(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for var in ("LLM_BASE_URL", "SLACK_WEBHOOK_URL"):  # save() exports what it writes; undo after the test
        monkeypatch.setenv(var, "")
    path = tmp_path / "overwatch.yaml"
    path.write_text(CONFIG)
    s = settings.read(path)
    assert [e["name"] for e in s["externals"]] == ["psp.example.com"]
    assert s["externals"][0]["callers"] == ["api"] and s["externals"][0]["log_text"] == "psp.example.com"

    s["externals"][0].update(health_url="http://psp.example.com/health", log_text="card declined")
    s["externals"].append({"name": "api.stripe.com", "original": "", "callers": ["api"], "health_url": "",
                           "log_text": "stripe", "enabled": True})
    s["checks"][0]["enabled"] = False
    s["notifiers"]["slack"] = {"enabled": True, "webhook_url": "https://hooks.slack.com/secret"}
    s["ai"]["base_url"] = "http://nightshift/v1"
    s["watch"]["interval_seconds"] = 7
    result = settings.save(path, s)

    text = path.read_text()
    assert text.startswith("# my system") and "hooks.slack.com" not in text and "nightshift" not in text
    env = (tmp_path / ".env").read_text()
    assert "SLACK_WEBHOOK_URL=https://hooks.slack.com/secret" in env and "LLM_BASE_URL=http://nightshift/v1" in env
    assert result["backup"]

    cfg = load_config(path)
    names = {c.name: c for c in cfg.checks}
    assert "api_up" not in names  # switched off
    assert names["card_errors"].query == "count api 2 card declined"  # updated in place, name kept
    assert names["psp_example_com_up"].url == "http://psp.example.com/health"
    assert names["api_stripe_com_errors"].query == "count api 2 stripe"
    assert cfg.topology["api"] == ["psp.example.com", "api.stripe.com"]
    assert cfg.interval_seconds == 7
    assert yaml.safe_load(text)["services"]["psp.example.com"]["normal"] == "40 ms"


def test_removing_an_external_drops_its_checks_and_edges(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "overwatch.yaml"
    path.write_text(CONFIG)
    s = settings.read(path)
    s["externals"] = []
    settings.save(path, s)
    cfg = load_config(path)
    assert "psp.example.com" not in cfg.services and "card_errors" not in {c.name for c in cfg.checks}
    assert "api" not in cfg.topology or cfg.topology["api"] == []


def test_probe_rejects_placeholder_token(monkeypatch):
    import httpx
    from overwatch import llm

    monkeypatch.setattr(httpx, "get", lambda *a, **k: type("R", (), {"status_code": 200})())
    out = llm.probe("http://127.0.0.1:8088/v1", "rt-local")
    assert not out["ok"] and "rt-local" in out["error"]
