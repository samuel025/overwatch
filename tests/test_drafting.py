"""Draft with AI: suggestions for empty descriptions only, from facts Overwatch holds, never secrets."""

import pytest

from overwatch.config import LLMConfig
from overwatch.drafting import draft, missing
from overwatch.llm import LLMError

CFG = LLMConfig(base_url="http://x", api_key="", investigator_model="deep", summary_model="deep", triage_model="fast")
TOPOLOGY = {"api": ["postgres", "redis"], "prometheus": ["api"]}
CHECKS = [{"name": "api_running", "service": "api"}, {"name": "postgres_running", "service": "postgres"}]
SERVICES = [
    {"name": "api", "what": "TODO: describe what this service does", "runs_on": "1 Docker container", "image": "shop-api",
     "env": {"DB_PASSWORD": "hunter2"}},                                   # extra keys must never reach the model
    {"name": "postgres", "what": "", "runs_on": "1 Docker container", "image": "postgres:16-alpine"},
    {"name": "redis", "what": "Session cache. Evictions are normal.", "runs_on": "1 Docker container", "image": "redis:7"},
]


class Scripted:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat_json(self, messages, model, agent, patience=None, **_):
        self.calls.append((model, messages))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_only_empty_and_todo_fields_are_drafted_and_a_persons_text_is_never_replaced():
    llm = Scripted([{"system": "Three services behind an API.", "services": {
        "api": "Application built from shop-api; it calls postgres and redis, and its purpose is not stated.",
        "postgres": "PostgreSQL database that the api calls.",
        "redis": "OVERWRITES what the person wrote"}}])
    out = draft(CFG, {"name": "shop", "description": "TODO: one or two sentences"}, SERVICES, TOPOLOGY, CHECKS, llm=llm)
    assert set(out["services"]) == {"api", "postgres"}          # redis already had a description: the model's answer is dropped
    assert out["system"] == "Three services behind an API."
    asked = llm.calls[0][1][1]["content"]
    assert "Describe these services: api, postgres." in asked and "redis" in asked  # redis is listed as a fact, not asked for


def test_no_secret_or_environment_value_is_sent_to_the_model():
    llm = Scripted([{"system": "", "services": {"api": "x", "postgres": "y"}}])
    draft(CFG, {"name": "shop", "description": "A real description."}, SERVICES, TOPOLOGY, CHECKS, llm=llm)
    sent = "\n".join(m["content"] for m in llm.calls[0][1])
    assert "hunter2" not in sent and "DB_PASSWORD" not in sent
    assert "image shop-api" in sent and "calls: postgres, redis" in sent and "called by: prometheus" in sent
    assert "watched by checks: api_running" in sent
    assert "Do not write a system description" in sent          # the system already had one


def test_nothing_missing_means_no_model_call():
    llm = Scripted([])
    full = [{**s, "what": "described"} for s in SERVICES]
    assert missing({"description": "ok"}, full) == (False, [])
    assert draft(CFG, {"name": "s", "description": "ok"}, full, TOPOLOGY, CHECKS, llm=llm) == {"system": None, "services": {}}
    assert llm.calls == []


def test_a_slow_fast_model_falls_back_to_the_main_model_and_a_total_failure_is_an_error():
    llm = Scripted([LLMError("timed out"), {"system": "", "services": {"api": "Application that calls postgres."}}])
    out = draft(CFG, {"name": "s", "description": "ok"}, SERVICES[:1], TOPOLOGY, CHECKS, llm=llm)
    assert out["services"] == {"api": "Application that calls postgres."}
    assert [c[0] for c in llm.calls] == ["fast", "deep"]
    with pytest.raises(LLMError):
        draft(CFG, {"name": "s", "description": "ok"}, SERVICES[:1], TOPOLOGY, CHECKS, llm=Scripted([LLMError("a"), LLMError("b")]))


def test_a_draft_that_is_itself_a_todo_or_too_long_is_cleaned():
    long = "word " * 200
    llm = Scripted([{"system": "", "services": {"api": "TODO: fill in", "postgres": long}}])
    out = draft(CFG, {"name": "s", "description": "ok"}, SERVICES, TOPOLOGY, CHECKS, llm=llm)
    assert "api" not in out["services"] and len(out["services"]["postgres"]) <= 260


def test_the_draft_endpoint_in_setup_mode_uses_the_provider_the_wizard_passes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from overwatch import drafting
    from overwatch.server import create_app

    for k in ("LLM_BASE_URL", "LLM_API_KEY", "OVERWATCH_MODEL", "OVERWATCH_TRIAGE_MODEL", "OVERWATCH_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    seen = {}

    def fake(cfg, system, services, topology, checks, llm=None):
        seen.update(base=cfg.base_url, model=cfg.investigator_model, fast=cfg.triage_model, topology=topology, n=len(services))
        return {"system": None, "services": {"api": "drafted"}}

    monkeypatch.setattr(drafting, "draft", fake)
    with TestClient(create_app(None, watch=False, config_path=tmp_path / "overwatch.yaml")) as c:
        # no AI provider anywhere: a clear message, not a crash
        r = c.post("/api/draft", json={"services": [{"name": "api", "what": ""}]})
        assert r.status_code == 400 and "AI provider" in r.json()["detail"]
        ok = c.post("/api/draft", json={"services": [{"name": "api", "what": ""}], "topology": {"api": ["db"]},
                                        "ai": {"base_url": "http://llm/v1", "model": "deep", "triage_model": "fast"}})
        assert ok.status_code == 200 and ok.json()["services"] == {"api": "drafted"}
        assert seen == {"base": "http://llm/v1", "model": "deep", "fast": "fast", "topology": {"api": ["db"]}, "n": 1}


def test_only_software_an_image_names_outright_is_identified_everything_else_is_not():
    from overwatch.drafting import identify

    assert identify("postgres:16-alpine") == "PostgreSQL database"
    assert identify("docker.io/library/redis:7") == "Redis cache / key-value store"
    assert identify("grafana/loki:3.2.0") == "Grafana Loki log store"
    assert identify("prom/prometheus:v2.54.1") == "Prometheus metrics server"
    assert identify("0xuell/drmq:latest") is None                    # "mq" inside a made-up name proves nothing
    assert identify("url_shortener_go-api") is None
    assert identify("") is None
    assert identify("myorg/app:postgres-14") is None                 # the tag is not the software


def test_the_model_is_told_which_services_are_identified_and_which_are_not():
    llm = Scripted([{"system": "", "services": {"api": "x", "drmq": "y"}}])
    svcs = [{"name": "api", "what": "", "image": "shop-api"}, {"name": "drmq", "what": "", "image": "0xuell/drmq:latest"},
            {"name": "db", "what": "described", "image": "postgres:16"}]
    draft(CFG, {"name": "s", "description": "ok"}, svcs, {}, [], llm=llm)
    sent = llm.calls[0][1][1]["content"]
    assert "drmq: image 0xuell/drmq:latest; software not identified" in sent
    assert "db: image postgres:16; identified as: PostgreSQL database" in sent
