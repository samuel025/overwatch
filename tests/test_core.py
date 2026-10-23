"""Offline tests: no database, no Nightshift, no memory service needed."""

from __future__ import annotations

import json

import pytest

from overwatch.agents import Investigator, Responder, Watcher
from overwatch.config import CheckConfig, Config, LLMConfig, load_config
from overwatch.llm import extract_json
from overwatch.memory.base import Memory
from overwatch.models import Anomaly, Incident
from overwatch.sources.base import DataSource, SourceError
from overwatch.sources.sql import check_read_only


class FakeSource(DataSource):
    language = "SQL"

    def __init__(self, answers: dict[str, list[dict]]):
        super().__init__("db", {})
        self.answers = answers
        self.seen: list[str] = []

    def query(self, q):
        check_read_only(q)
        self.seen.append(q)
        for key, rows in self.answers.items():
            if key in q:
                return rows
        raise SourceError(f"relation does not exist: {q[:30]}")


class ScriptedLLM:
    """Stands in for Nightshift: returns the next scripted reply on each call."""

    def __init__(self, replies: list[dict]):
        self.replies = [json.dumps(r) for r in replies]
        self.prompts: list[list[dict]] = []

    def chat_json(self, messages, model, agent, **_):
        self.prompts.append(list(messages))
        return extract_json(self.replies.pop(0))


class RecordingMemory(Memory):
    name = "recording"

    def __init__(self):
        self.stored, self.feedbacks = [], []

    def remember(self, incident):
        self.stored.append(incident.id)
        return "mem-9"

    def recall(self, symptoms, top_n=3):
        return []

    def feedback(self, memory_id, kind):
        self.feedbacks.append((memory_id, kind))


def make_config(**kw) -> Config:
    return Config(
        system_name="t", system_description="", sources={"db": {}}, catalog="t(x)", checks=[],
        llm=LLMConfig(base_url="http://x", api_key="", investigator_model="m", summary_model="m", max_steps=kw.get("max_steps", 5)),
    )


@pytest.mark.parametrize("sql", ["SELECT 1", "with a as (select 1) select * from a", "select * from t;  ", "-- c\nSELECT 1"])
def test_read_only_allows_selects(sql):
    check_read_only(sql)


@pytest.mark.parametrize(
    "sql", ["DROP TABLE t", "select 1; delete from t", "update t set x=1", "with a as (delete from t returning *) select * from a"]
)
def test_read_only_blocks_writes(sql):
    with pytest.raises(SourceError):
        check_read_only(sql)


def test_extract_json_handles_fences_and_thinking():
    assert extract_json('<think>hmm {"no": 1}</think>```json\n{"action": "query"}\n```') == {"action": "query"}
    assert extract_json('Sure! {"a": {"b": "}"}} trailing') == {"a": {"b": "}"}}
    assert extract_json("no json here") is None


def test_watcher_flags_threshold_and_broken_checks():
    src = FakeSource({"latency": [{"value": 900}], "errors": [{"rate": 0.01}]})
    checks = [
        CheckConfig("lat", "db", "select latency", 500, service="orders"),
        CheckConfig("err", "db", "select errors", 0.05, service="payments"),
        CheckConfig("broken", "db", "select missing", 1, service="gateway"),
    ]
    found = {a.check: a for a in Watcher(checks, {"db": src}).run()}
    assert set(found) == {"lat", "broken"}
    assert found["lat"].value == 900
    assert found["broken"].comparison == "error"


def test_watcher_no_data_alerts_only_when_configured():
    src = FakeSource({"empty": [{"value": None}]})
    checks = [
        CheckConfig("quiet", "db", "select empty", 1, service="a"),
        CheckConfig("silent", "db", "select empty", 1, service="b", no_data="alert"),
    ]
    found = Watcher(checks, {"db": src}).run()
    assert [a.check for a in found] == ["silent"] and found[0].comparison == "no_data"


def test_watcher_suspects_first_and_confirms_on_the_second_pass():
    src = FakeSource({"latency": [{"value": 900}]})
    w = Watcher([CheckConfig("lat", "db", "select latency", 500, service="orders")], {"db": src}, confirm_samples=2)
    assert w.run() == [] and w.suspected == {"lat"}  # one bad sample: shown, not paged
    [a] = w.run()
    assert a.check == "lat" and w.suspected == set()
    src.answers = {"latency": [{"value": 10}]}
    assert w.run() == [] and w.suspected == set() and w.streak["lat"] == 0


def test_probe_check_calls_the_health_url(monkeypatch):
    import httpx as _httpx

    class R:
        status_code = 200

    calls = []
    monkeypatch.setattr(_httpx, "get", lambda url, timeout: calls.append(url) or R())
    probe = CheckConfig("orders_up", kind="probe", url="http://orders/health", threshold=1, comparison="<", service="orders")
    w = Watcher([probe], {})
    assert w.run() == [] and calls == ["http://orders/health"]

    def down(url, timeout):
        raise _httpx.ConnectError("refused")

    monkeypatch.setattr(_httpx, "get", down)
    [a] = w.run()
    assert a.value == 0 and a.service == "orders"


def test_investigator_loop_collects_evidence_and_concludes():
    src = FakeSource({"request_metrics": [{"p95": 1700}], "deploys": [{"version": "v1.8.0"}]})
    llm = ScriptedLLM([
        {"thought": "check latency", "action": "query", "source": "db", "query": "select * from request_metrics", "purpose": "latency"},
        {"thought": "typo", "action": "query", "source": "nope", "query": "select 1", "purpose": "bad source"},
        {"thought": "deploys?", "action": "query", "source": "db", "query": "select * from deploys", "purpose": "recent deploys"},
        {"thought": "done", "action": "conclude", "root_cause": "v1.8.0 dropped an index", "confidence": 0.9,
         "severity": "high", "memory_used": ["mem-1"], "recommended_actions": ["recreate index"],
         "evidence": [{"step": 1, "quote": "1700", "shows": "slow"}, {"step": 3, "quote": "v1.8.0", "shows": "deploy"}]},
    ])
    inc = Incident(anomalies=[Anomaly("lat", "orders", 1700, 250, ">", "p95")])
    inc.similar_incidents = [{"id": "mem-1", "content": "index dropped before", "relevance": 0.6}]
    Investigator(make_config(), llm, {"db": src}).investigate(inc)

    assert inc.root_cause == "v1.8.0 dropped an index"
    assert inc.confidence == 0.9 and inc.severity == "high"
    assert len(inc.evidence) == 3 and inc.evidence[1].error.startswith("unknown source")
    assert inc.evidence[0].note.startswith("[key]") and inc.evidence[2].note.startswith("[key]")
    assert inc.similar_incidents[0]["used"] is True
    assert "index dropped before" in llm.prompts[0][1]["content"]  # memory reached the model
    assert "ERROR unknown source" in llm.prompts[2][-1]["content"]  # errors are fed back


def test_investigator_forces_conclusion_after_the_query_budget():
    src = FakeSource({"x": [{"v": 1}]})
    q = {"thought": "", "action": "query", "source": "db", "query": "select x", "purpose": ""}
    llm = ScriptedLLM([q, q, {"action": "conclude", "root_cause": "best guess", "confidence": 0.3}])
    inc = Incident(anomalies=[Anomaly("c", "s", 1, 0, ">", "")])
    Investigator(make_config(max_steps=2), llm, {"db": src}).investigate(inc)
    assert inc.root_cause == "best guess" and len(inc.evidence) == 2
    assert "Conclude now" in llm.prompts[-1][-1]["content"]
    assert "at most 2 queries" in llm.prompts[0][1]["content"]


def test_investigator_that_never_concludes_fails_instead_of_asking_for_approval():
    src = FakeSource({"x": [{"v": 1}]})
    q = {"thought": "", "action": "query", "source": "db", "query": "select x", "purpose": ""}
    llm = ScriptedLLM([q, q, q])  # ignores the final instruction and queries again
    inc = Incident(anomalies=[Anomaly("c", "s", 1, 0, ">", "")])
    Investigator(make_config(max_steps=2), llm, {"db": src}).investigate(inc)
    assert inc.status == "failed" and "no conclusion after 2 queries" in inc.root_cause
    assert len(inc.evidence) == 2  # the forced final turn never runs a query


def test_responder_learns_only_from_human_decision():
    mem = RecordingMemory()
    inc = Incident(anomalies=[Anomaly("c", "s", 1, 0, ">", "")], root_cause="x")
    inc.similar_incidents = [{"id": "mem-1", "used": True}, {"id": "mem-2", "used": False}]
    Responder([], mem).approve(inc)
    assert inc.status == "reported" and mem.stored == [inc.id] and mem.feedbacks == [("mem-1", "helpful")]

    mem2 = RecordingMemory()
    inc2 = Incident(anomalies=inc.anomalies, similar_incidents=[{"id": "mem-1", "used": True}])
    Responder([], mem2).dismiss(inc2)
    assert inc2.status == "dismissed" and mem2.stored == [] and mem2.feedbacks == [("mem-1", "not_helpful")]


def test_config_expands_env_and_validates(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_URL", "sqlite://")
    (tmp_path / "ow.yaml").write_text(
        "sources:\n  db: {type: sql, url: '${DB_URL}'}\n"
        "checks:\n  - {name: c, source: db, query: select 1, threshold: 1}\n"
        "llm: {base_url: '${NS:-http://127.0.0.1:8088/v1}', investigator_model: m}\n"
    )
    cfg = load_config(tmp_path / "ow.yaml")
    assert cfg.sources["db"]["url"] == "sqlite://"
    assert cfg.llm.base_url == "http://127.0.0.1:8088/v1"
    assert cfg.memory["type"] == "hmem"

    (tmp_path / "bad.yaml").write_text("sources: {}\nchecks:\n  - {name: c, source: nope, query: q, threshold: 1}\n")
    with pytest.raises(ValueError, match="unknown source"):
        load_config(tmp_path / "bad.yaml")


def test_model_failure_marks_incident_failed_not_approvable():
    from overwatch.llm import LLMError

    class DownLLM:
        def chat_json(self, *a, **k):
            raise LLMError("cannot reach the AI provider")

    events = []
    inc = Incident(anomalies=[Anomaly("c", "s", 1, 0, ">", "")])
    Investigator(make_config(), DownLLM(), {}, emit=lambda t, **d: events.append(t)).investigate(inc)
    assert inc.status == "failed"
    assert inc.root_cause.startswith("Investigation failed")
    assert "investigator.failed" in events


def test_watcher_relative_check_compares_to_its_own_normal():
    import time as _t

    src = FakeSource({"lat": [{"value": 12}]})
    check = CheckConfig("lat", "db", "select lat", None, service="orders", relative_factor=4, relative_min=30)
    w = Watcher([check], {"db": src})
    old = _t.time() - 600
    for i in range(20):  # ten minutes of normal ~12ms
        w.history["lat"].append((old + i * 20, 12.0))
    assert w.run() == []  # 12ms is normal
    src.answers = {"lat": [{"value": 75}]}
    [a] = w.run()
    assert a.comparison == "x_normal" and a.threshold == 48 and "6.2x" in a.description
    src.answers = {"lat": [{"value": 20}]}
    assert w.run() == []  # below relative_min: never alert on tiny absolute numbers


def _grounded_incident(root_cause, cited, rows=None):
    from overwatch.models import Evidence

    inc = Incident(anomalies=[Anomaly("lat", "orders", 900, 250, ">", "p95")], root_cause=root_cause)
    inc.evidence = [Evidence("db", "select 1", rows or [{"instance": "orders", "status": "exited", "exit_code": 0}])]
    inc.cited = cited
    return inc


def test_grounding_verifies_quotes_against_the_cited_step():
    from overwatch import grounding

    inc = _grounded_incident("The orders container exited.", [
        {"step": 1, "quote": "exited"}, {"step": 1, "quote": "0"}, {"step": 1, "quote": "running"}])
    g = grounding.check(inc, "system description")
    assert g["verified"] == 2 and g["total"] == 3 and g["unverified"] == [{"step": 1, "quote": "running"}]


def test_grounding_flags_infrastructure_the_data_never_mentioned():
    from overwatch import grounding

    inc = _grounded_incident(
        "Orders v1.9.0 failed its liveness probe, so Kubernetes restarted the pod.",
        [{"step": 1, "quote": "exited"}])
    g = grounding.check(inc, "orders runs on Docker Compose")
    assert "Kubernetes" in g["unsupported_terms"] and "v1.9.0" in g["unsupported_terms"]
    assert not g["ok"]


def test_well_grounded_conclusion_keeps_its_confidence_and_badly_grounded_is_capped():
    src = FakeSource({"containers": [{"instance": "orders", "status": "exited"}]})
    q = {"action": "query", "source": "db", "query": "select * from containers", "purpose": "state"}
    good = {"action": "conclude", "root_cause": "The orders container exited.", "confidence": 0.9,
            "evidence": [{"step": 1, "quote": "exited"}]}
    bad = {"action": "conclude", "root_cause": "A Kubernetes liveness probe killed orders.", "confidence": 0.9,
           "evidence": [{"step": 1, "quote": "probe failed"}]}
    for reply, expected in ((good, 0.9), (bad, 0.6)):
        inc = Incident(anomalies=[Anomaly("c", "orders", 1, 0, ">", "")])
        Investigator(make_config(), ScriptedLLM([q, reply]), {"db": src}).investigate(inc)
        assert inc.confidence == expected, (reply["root_cause"], inc.grounding)


def test_disagreeing_with_the_first_guess_without_a_reason_lowers_confidence():
    src = FakeSource({"x": [{"state": "exited"}]})
    reply = {"action": "conclude", "root_cause": "orders exited", "confidence": 0.9,
             "evidence": [{"step": 0, "quote": "exited"}], "first_guess": {"verdict": "disagree", "why": ""}}
    inc = Incident(anomalies=[Anomaly("c", "orders", 1, 0, ">", "")])
    from overwatch.models import Evidence

    inc.pack = [Evidence("db", "select x", [{"state": "exited"}], note="[pack] state")]
    inc.hypothesis = {"text": "orders is slow", "confidence": 0.7}
    Investigator(make_config(), ScriptedLLM([reply]), {"db": src}).investigate(inc)
    assert inc.confidence == 0.5 and "competing explanations" in inc.reasoning[-1]


def test_grounding_accepts_key_value_quotes_of_real_rows():
    from overwatch import grounding

    inc = _grounded_incident("orders exited.", [
        {"step": 1, "quote": "ts: 2026-10-23T08:39:39.671804, requests: 145, rps: 14.5, error_rate: 0.0"},
        {"step": 1, "quote": "requests: 999, rps: 99.9"}],
        rows=[{"ts": "2026-10-23T08:39:39.671804+00:00", "requests": 145, "rps": 14.5, "error_rate": 0.0}])
    g = grounding.check(inc, "")
    assert g["verified"] == 1 and g["unverified"][0]["quote"].startswith("requests: 999")


def test_grounding_counts_the_timeline_as_step_zero():
    from overwatch import grounding

    inc = _grounded_incident("orders stopped at 08:39:45 UTC.", [{"step": 0, "quote": "container orders exited (exit code 0)"}])
    g = grounding.check(inc, "- Pushed event at 08:39:45 UTC: container orders exited (exit code 0) at 08:39:45 UTC")
    assert g["verified"] == 1 and g["unsupported_terms"] == []


def test_grounding_handles_empty_results_prose_around_values_and_ordinary_hyphenated_words():
    from overwatch import grounding
    from overwatch.models import Evidence

    inc = _grounded_incident("No host-level event stopped payments-b or config-99.", [
        {"step": 1, "quote": "0 rows"},
        {"step": 2, "quote": "error_rate: 0.8102189781021898 at 08:47:45, then 1.0 thereafter"},
        {"step": 3, "quote": 'service: "orders", status: "exited" while gateway, payments-a all "running"'}])
    inc.evidence = [
        Evidence("db", "q1", []),
        Evidence("db", "q2", [{"ts": "2026-10-23T08:47:45+00:00", "error_rate": 0.8102189781021898},
                              {"ts": "2026-10-23T08:47:55+00:00", "error_rate": 1.0}]),
        Evidence("db", "q3", [{"service": "orders", "status": "exited"}, {"service": "gateway", "status": "running"},
                              {"service": "payments-a", "status": "running"}]),
    ]
    g = grounding.check(inc, "payments-b")
    assert g["verified"] == 3, g["unverified"]
    assert g["unsupported_terms"] == ["config-99"]  # "host-level" is English; config-99 is an id not in the data
