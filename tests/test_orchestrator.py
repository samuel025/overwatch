"""Incident lifecycle: background investigation, merging, recovery, persistence, console auth.

Uses a real SQLite file as the data source and a scripted model, so no services are needed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from overwatch.config import CheckConfig, Config, LLMConfig
from overwatch.llm import extract_json
from overwatch.orchestrator import Overwatch
from overwatch.server import create_app


class SlowScriptedLLM:
    """Concludes after `delay` seconds; `gate` lets a test hold the investigation open."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.gate = threading.Event()
        self.gate.set()

    def chat_json(self, messages, model, agent):
        self.gate.wait(5)
        time.sleep(self.delay)
        return extract_json(json.dumps({"action": "conclude", "root_cause": "x broke", "confidence": 0.8}))


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "metrics.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE m (name TEXT PRIMARY KEY, value REAL)")
    conn.executemany("INSERT INTO m VALUES (?, ?)", [("a", 1), ("b", 1)])
    conn.commit()

    def set_value(name, value):
        conn.execute("UPDATE m SET value = ? WHERE name = ?", (value, name))
        conn.commit()

    def make(**overrides) -> Overwatch:
        cfg = Config(
            system_name="t", system_description="", sources={"db": {"type": "sql", "url": f"sqlite:///{db}"}},
            catalog="m(name, value)",
            checks=[
                CheckConfig("a_high", "db", "SELECT value FROM m WHERE name = 'a'", 10, service="a"),
                CheckConfig("b_high", "db", "SELECT value FROM m WHERE name = 'b'", 10, service="b"),
            ],
            llm=LLMConfig(base_url="http://unused", api_key="", investigator_model="m", summary_model="m"),
            memory={"type": "none"}, notifiers=[], trace_dir=str(tmp_path / "traces"),
            **{"cooldown_seconds": 0, "resolve_after_seconds": 0, "confirm_samples": 1, "event_grace_seconds": 0,
               **overrides},
        )
        ow = Overwatch(cfg)
        ow.investigator.llm = SlowScriptedLLM()
        return ow

    yield make, set_value
    conn.close()


def wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_watching_does_not_wait_for_the_investigation(world):
    make, set_value = world
    ow = make()
    ow.investigator.llm.gate.clear()  # hold the investigation open
    set_value("a", 50)
    started = time.time()
    inc = ow.check_once()
    assert time.time() - started < 1 and inc.status == "investigating"
    assert ow.check_once() is None  # the next pass runs while the investigation is still going
    ow.investigator.llm.gate.set()
    assert wait_for(lambda: inc.status == "awaiting_approval")
    ow.stop()


def test_a_cascade_becomes_one_incident(world):
    make, set_value = world
    ow = make()
    set_value("a", 50)
    first = ow.check_once()
    set_value("b", 50)
    assert ow.check_once() is first
    assert [a.check for a in first.anomalies] == ["a_high", "b_high"]
    assert len(ow.incidents) == 1
    ow.stop()


def test_incident_recovers_when_checks_stay_healthy(world):
    make, set_value = world
    ow = make()
    set_value("a", 50)
    inc = ow.check_once()
    assert wait_for(lambda: inc.status == "awaiting_approval")
    ow.check_once()
    assert inc.recovered_at is None  # still failing
    set_value("a", 1)
    ow.check_once()
    assert inc.recovered_at is not None
    assert inc.blast_radius["recovered_at"] is not None
    ow.stop()


def test_recovery_waits_out_flapping(world):
    make, set_value = world
    ow = make(**{"resolve_after_seconds": 60})
    set_value("a", 50)
    inc = ow.check_once()
    set_value("a", 1)
    ow.check_once()  # healthy, but not for long enough
    set_value("a", 50)
    ow.check_once()  # flapped back: the healthy clock resets
    assert inc.recovered_at is None and inc.id not in ow.healthy_since
    ow.stop()


def test_a_person_can_resolve_a_reported_problem(world):
    make, _ = world
    ow = make()
    inc = ow.investigate_symptom("checkout feels slow")
    ow._track_recovery(set())
    assert inc.recovered_at is None  # human reports never auto-recover
    ow.resolve(inc.id)
    assert inc.recovered_at is not None and "Marked resolved" in inc.reasoning[-1]
    ow.stop()


def test_incidents_survive_a_restart(world):
    make, set_value = world
    ow = make()
    ow.investigator.llm.gate.clear()
    set_value("a", 50)
    inc = ow.check_once()
    ow.stop()  # stopped mid-investigation

    again = make()
    restored = again.incidents[inc.id]
    assert restored.status == "failed" and "restarted" in restored.root_cause
    assert restored.anomalies[0].check == "a_high"
    again.stop()


def test_console_requires_the_token(world):
    make, _ = world
    ow = make(console_token="s3cret")
    with TestClient(create_app(ow, watch=False)) as c:
        assert c.get("/api/status").status_code == 401
        assert c.get("/api/status", headers={"authorization": "Bearer s3cret"}).status_code == 200
        r = c.get("/?token=s3cret", follow_redirects=False)
        assert r.status_code == 303 and "ow_token" in r.headers["set-cookie"]
        assert c.get("/api/status").status_code == 200  # the cookie now authenticates
    with TestClient(create_app(ow, watch=False), cookies={"ow_token": "wrong"}) as other:
        assert other.get("/api/status").status_code == 401
    ow.stop()


def test_failed_investigation_still_recovers(world):
    from overwatch.llm import LLMError

    class Down:
        def chat_json(self, *a, **k):
            raise LLMError("provider unreachable")

    make, set_value = world
    ow = make()
    ow.investigator.llm = Down()
    set_value("a", 50)
    inc = ow.check_once()
    assert wait_for(lambda: inc.status == "failed")
    set_value("a", 1)
    ow.check_once()
    assert inc.recovered_at is not None
    ow.stop()


def test_container_event_marks_down_and_opens_an_incident_at_once(world):
    make, _ = world
    ow = make()
    ow.on_container_event("down", "a", "a-1", "container a-1 exited (exit code 137)")
    assert "a-1" in ow.status()["down"]
    [inc] = ow.incidents.values()
    assert inc.anomalies[0].check == "event:a-1_down" and inc.anomalies[0].comparison == "event"
    assert wait_for(lambda: inc.status == "awaiting_approval")
    ow.check_once()
    assert inc.recovered_at is None  # still down: the event keeps it failing
    ow.on_container_event("up", "a", "a-1", "container a-1 started")
    ow.check_once()
    assert inc.recovered_at is not None
    ow.stop()


def test_evidence_pack_runs_first_and_reaches_the_model(world):
    from overwatch.config import EvidenceConfig

    make, set_value = world
    ow = make(evidence=[EvidenceConfig("vals", "All values", "db", "SELECT name, value FROM m")])
    seen = []
    original = ow.investigator.llm.chat_json
    ow.investigator.llm.chat_json = lambda messages, model, agent: seen.append(messages) or original(messages, model, agent)
    set_value("a", 50)
    inc = ow.handle(ow.watcher.run())
    assert [e.note for e in inc.pack] == ["[pack] All values", "[pack] Changes reported to Overwatch in the last hour"]
    assert len(inc.pack[0].rows) == 2
    assert "Evidence already gathered automatically" in seen[0][1]["content"]
    ow.stop()


def test_fast_first_guess_is_kept_and_passed_on(world):
    make, set_value = world
    ow = make()
    ow.cfg.llm.triage_model = "fast-model"
    ow.cfg.llm.critic = False  # this test is about the first guess; the critic has its own tests

    class Both:
        def __init__(self):
            self.models = []

        def chat_json(self, messages, model, agent, **_):
            self.models.append(model)
            if model == "fast-model":
                return {"hypothesis": "a is overloaded", "confidence": 0.6, "origin_service": "a"}
            assert "first guess" in messages[1]["content"]
            return {"action": "conclude", "root_cause": "a is overloaded, confirmed", "confidence": 0.9}

    ow.investigator.llm = Both()
    set_value("a", 50)
    inc = ow.handle(ow.watcher.run())
    assert ow.investigator.llm.models == ["fast-model", "m"]
    assert inc.hypothesis["text"] == "a is overloaded" and inc.root_cause.endswith("confirmed")
    ow.stop()


def test_pushed_alerts_open_incidents_that_a_person_closes(world):
    make, _ = world
    ow = make()
    inc = ow.ingest_alert("alertmanager", "a", "HighLatency", "p99 above 2s")
    assert inc.anomalies[0].check == "alert:alertmanager:HighLatency"
    assert wait_for(lambda: inc.status == "awaiting_approval")
    ow.check_once()
    assert inc.recovered_at is None  # no check behind it: never auto-recovers
    with TestClient(create_app(ow, watch=False)) as c:
        r = c.post("/api/ingest", json={"alerts": [{"status": "firing", "labels": {"alertname": "Down", "service": "b"}}]})
        assert r.status_code == 200 and r.json()["incidents"] == [inc.id]  # merged into the open incident
    ow.stop()


def test_a_quick_restart_is_not_an_outage(world):
    make, _ = world
    ow = make(event_grace_seconds=0.3)
    ow.on_container_event("down", "a", "a-1", "container a-1 exited (exit code 0)")
    assert "a-1" in ow.status()["down"]  # shown on the map at once
    ow.on_container_event("up", "a", "a-1", "container a-1 started")
    time.sleep(0.5)
    assert ow.incidents == {} and ow.status()["down"] == {}
    ow.on_container_event("down", "a", "a-1", "container a-1 exited (exit code 137)")
    assert wait_for(lambda: len(ow.incidents) == 1)  # stayed down past the grace period
    ow.stop()


def test_timeline_reaches_the_model_and_pack_rows_get_minutes_before_start(world):
    from overwatch.config import EvidenceConfig

    make, set_value = world
    ow = make(evidence=[EvidenceConfig("changes", "Changes", "db", "SELECT '2026-01-01T00:00:00+00:00' AS ts, 'x' AS name")])
    seen = []
    original = ow.investigator.llm.chat_json
    ow.investigator.llm.chat_json = lambda m, model, agent, **k: seen.append(m) or original(m, model, agent)
    ow.watcher.history["a_high"].extend((time.time() - 600 + i * 30, 1.0) for i in range(15))  # 10 min of normal
    set_value("a", 50)
    inc = ow.handle(ow.watcher.run())
    prompt = seen[0][1]["content"]
    assert "Incident timeline" in prompt and "The problem started at" in prompt
    assert "a_high [a]: normal 1 -> now 50 (FIRING)" in prompt
    assert inc.started_at and inc.pack[0].rows[0]["minutes_before_start"] > 0
    ow.stop()


def test_reported_changes_are_queryable_through_the_built_in_source(world):
    make, _ = world
    ow = make()
    ow.record_change("a", "v2.4.0", description="Release 2.4.0")
    rows = ow.sources["overwatch"].query("SELECT service, version FROM changes")
    assert rows == [{"service": "a", "version": "v2.4.0"}]
    import pytest as _pytest
    from overwatch.sources.base import SourceError

    with _pytest.raises(SourceError):
        ow.sources["overwatch"].query("DELETE FROM changes")
    with TestClient(create_app(ow, watch=False)) as c:
        assert c.post("/api/changes", json={"service": "b", "version": "1.0"}).status_code == 200
    assert len(ow.sources["overwatch"].query("SELECT * FROM changes")) == 2
    ow.stop()


def test_every_check_value_is_kept_and_queryable_by_the_investigator(world):
    make, set_value = world
    ow = make()
    ow.check_once()
    set_value("a", 50)  # a_high now fails
    ow.check_once()
    rows = ow.store.rows("SELECT check_name, value, failing FROM check_samples ORDER BY ts, check_name")
    assert ("a_high", 1.0, 0) in rows and ("a_high", 50.0, 1) in rows and ("b_high", 1.0, 0) in rows
    # the Investigator reads the same history through the built-in `overwatch` source
    assert ow.sources["overwatch"].query("SELECT count(*) AS n FROM check_samples WHERE check_name = 'a_high'") == [{"n": 2}]
    assert "check_samples(" in ow.cfg.catalog and "reported_changes" in {e.name for e in ow.evidence}


def test_own_history_is_read_through_zetaris_when_it_is_the_data_layer(world, monkeypatch):
    make, _ = world
    ow = make()
    assert ow.own_data == {"source": "overwatch", "prefix": "", "dialect": "sqlite"}
    ow.store.dialect = "postgresql"  # as if state_url pointed at Postgres
    assert ow._own_data_target(ow.cfg)["dialect"] == "postgresql"  # no Zetaris: read it directly
    monkeypatch.setenv("ZETARIS_JDBC_URL", "jdbc:zetaris://x")
    ow.cfg.sources["zetaris"] = {"type": "zetaris", "jdbc_url": "jdbc:zetaris://x", "driver_class": "com.Z"}
    assert ow._own_data_target(ow.cfg) == {"source": "zetaris", "prefix": "overwatch.", "dialect": "spark"}


def test_docker_telemetry_is_stored_as_tables_and_pruned(tmp_path):
    from datetime import datetime, timedelta, timezone

    from overwatch.store import IncidentStore
    from overwatch.telemetry import iso

    assert iso("2026-10-23T19:10:40.105317044Z") == "2026-10-23T19:10:40.105317+00:00"
    store = IncidentStore(tmp_path / "o.db")
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="microseconds")
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    store.add_container_logs([
        {"ts": old, "service": "db", "container": "db", "line": "checkpoint complete", "important": 0},
        {"ts": old, "service": "db", "container": "db", "line": "received fast shutdown request", "important": 1},
        {"ts": now, "service": "db", "container": "db", "line": "ready", "important": 0}])
    store.add_container_state([{"ts": now, "service": "db", "container": "db", "state": "exited", "exit_code": 0,
                                "oom_killed": 0, "restarts": 0, "health": None, "started_at": now, "finished_at": now}])
    store.add_container_events([{"ts": now, "service": "db", "container": "db", "action": "kill", "exit_code": None,
                                 "detail": "signal 15"}])
    store.prune_telemetry()
    lines = [r[0] for r in store.rows("SELECT line FROM container_logs ORDER BY ts, line")]
    assert lines == ["received fast shutdown request", "ready"]  # routine lines expire after an hour, important ones last
    assert store.rows("SELECT state FROM container_state") == [("exited",)]
    assert store.rows("SELECT detail FROM container_events") == [("signal 15",)]


def test_stopping_overwatch_closes_its_data_sources(world):
    make, _ = world
    ow = make()
    closed = []
    ow.sources["db"].close = lambda: closed.append("db")  # a Zetaris source holds a Java connection that blocks exit
    ow.stop()
    assert closed == ["db"]


def test_a_problem_that_starts_after_the_diagnosis_gets_a_fresh_investigation(world):
    make, set_value = world
    ow = make()
    calls = []
    real = ow.investigator.llm.chat_json
    ow.investigator.llm.chat_json = lambda *a, **k: calls.append(1) or real(*a, **k)
    set_value("a", 50)
    first = ow.check_once()
    assert wait_for(lambda: first.status == "awaiting_approval")
    before = len(calls)
    set_value("b", 50)
    assert ow.check_once() is first  # same incident, but its evidence predates b
    assert wait_for(lambda: len(calls) > before and first.status == "awaiting_approval")
    assert any("investigating again" in line for line in first.reasoning)
    ow.stop()


def test_investigating_again_is_limited_and_never_touches_an_incident_someone_acted_on(world):
    make, set_value = world
    ow = make()
    set_value("a", 50)
    first = ow.check_once()
    assert wait_for(lambda: first.status == "awaiting_approval")
    first.status = "dismissed"
    set_value("b", 50)
    ow.check_once()
    assert first.status == "dismissed" and ow._refreshes.get(first.id, 0) == 0
    ow.stop()


def _heal_and_break_again(make, set_value, **overrides):
    ow = make(**overrides)
    set_value("a", 50)
    set_value("b", 50)
    first = ow.check_once()
    assert wait_for(lambda: first.status == "awaiting_approval")
    set_value("a", 1)  # a recovers; b keeps failing, so the first incident never recovers and stays open
    for _ in range(3):
        ow.check_once()
    assert not first.recovered_at
    set_value("a", 50)  # a breaks again
    result = None
    for _ in range(3):
        result = ow.check_once() or result
    return ow, first, result


def test_a_check_that_healed_and_broke_again_is_not_swallowed_by_an_old_open_incident(world):
    make, set_value = world
    ow, first, second = _heal_and_break_again(make, set_value, merge_window_seconds=0)  # the old incident is out of the window
    assert second is not None and second is not first and len(ow.incidents) == 2
    ow.stop()


def test_inside_the_merge_window_that_problem_joins_the_incident_and_it_is_investigated_again(world):
    make, set_value = world
    ow, first, second = _heal_and_break_again(make, set_value)
    assert second is first and len(ow.incidents) == 1  # one incident, not two
    assert wait_for(lambda: any("investigating again" in line for line in first.reasoning))
    ow.stop()


def test_a_check_that_never_healed_stays_covered_by_its_open_incident(world):
    make, set_value = world
    ow = make()
    set_value("a", 50)
    first = ow.check_once()
    assert wait_for(lambda: first.status == "awaiting_approval")
    for _ in range(3):
        assert ow.check_once() is None  # still failing, still the same incident
    assert len(ow.incidents) == 1
    ow.stop()


def test_the_timeline_the_investigator_reads_lists_the_recent_change_as_a_suspect(world):
    make, set_value = world
    ow = make()
    ow.record_change("a", version="v2.0.0", kind="deploy", description="rewrote the lookup")
    ow.record_change("a", version="config-9", kind="config", description="old", ts="2020-01-01T00:00:00+00:00")  # ancient
    set_value("a", 50)
    inc = ow.check_once()
    lines = ow.build_timeline(inc)
    text = "\n".join(lines)
    assert "Suspect events" in text and "S1" in text and "v2.0.0" in text and "rewrote the lookup" in text
    assert "config-9" not in text  # outside the hour before the problem
    ow.stop()


def _critic_world(world, reply):
    make, set_value = world
    ow = make()
    ow.cfg.llm.triage_model = "fast-model"

    class Scripted:
        def __init__(self):
            self.agents = []

        def chat_json(self, messages, model, agent, **_):
            self.agents.append(agent)
            if agent == "critic":
                if isinstance(reply, Exception):
                    raise reply
                assert "Diagnosis:" in messages[1]["content"] and "Data:" in messages[1]["content"]
                return reply
            if agent == "triage":
                return {"hypothesis": "guess", "confidence": 0.5}
            return {"action": "conclude", "root_cause": "The container failed its health check and was stopped.",
                    "confidence": 0.9}

    ow.investigator.llm = Scripted()
    set_value("a", 50)
    return ow, ow.handle(ow.watcher.run())


def test_the_critic_flags_claims_whose_quoted_support_is_not_in_the_data(world):
    reply = {"claims": [
        {"claim": "The container failed its health check", "support": "health_status: unhealthy at 06:19"},  # not in the data
        {"claim": "a_high is failing", "support": "a_high on a"},                                        # in the anomalies
        {"claim": "A cold cache slowed the scans", "support": ""}]}                                      # no support at all
    ow, inc = _critic_world(world, reply)
    assert inc.grounding["unsupported_claims"] == ["The container failed its health check", "A cold cache slowed the scans"]
    assert inc.confidence == 0.6 and any("second reader" in r for r in inc.reasoning)
    assert "critic" in ow.investigator.llm.agents
    ow.stop()


def test_a_diagnosis_whose_claims_all_quote_the_data_is_left_alone(world):
    ow, inc = _critic_world(world, {"claims": [{"claim": "a_high is failing", "support": "a_high on a"}]})
    assert inc.grounding["unsupported_claims"] == []
    assert not any("second reader" in r for r in inc.reasoning)  # nothing flagged, nothing added
    ow.stop()


def test_a_critic_failure_never_fails_the_investigation(world):
    from overwatch.llm import LLMError

    ow, inc = _critic_world(world, LLMError("model down"))
    assert inc.status == "awaiting_approval" and inc.root_cause
    assert any("critic unavailable" in r for r in inc.reasoning)
    ow.stop()
