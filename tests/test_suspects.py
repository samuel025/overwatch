"""Ranked suspects: the timing facts a model would otherwise guess."""

from datetime import datetime, timezone

from overwatch import suspects as sus
from overwatch.agents.investigator import Investigator
from overwatch.models import Incident

T0 = datetime(2026, 10, 24, 6, 20, 0, tzinfo=timezone.utc).timestamp()  # the problem began at 06:20:00


def at(minutes: float) -> str:
    return datetime.fromtimestamp(T0 + minutes * 60, timezone.utc).isoformat()


def test_timestamps_in_the_formats_systems_use_all_parse():
    assert sus.parse_ts("2026-10-24T06:20:00+00:00") == T0
    assert sus.parse_ts("2026-10-24 06:20:00.000000") == T0  # Zetaris / Postgres, no zone: UTC
    assert sus.parse_ts("2026-10-24T06:20:00Z") == T0
    assert sus.parse_ts("tomorrow") is None and sus.parse_ts(None) is None


def test_change_rows_with_unfamiliar_column_names_still_become_suspects():
    [s] = sus.from_changes([{"released_at": at(-1), "app": "billing", "tag": "v9", "message": "switch to new tax API"}])
    assert (s.service, s.kind) == ("billing", "deploy") and "v9" in s.what and "new tax API" in s.what
    [c] = sus.from_changes([{"ts": at(-1), "service": "gw", "version": "config-81"}])
    assert c.kind == "config"
    [anything] = sus.from_changes([{"when_it_happened": at(-1), "svc": "x"}])  # time column not recognised: first parsable value
    assert anything.ts == T0 - 60
    assert sus.from_changes([{"service": "gw", "note": "no time at all"}]) == []


def test_a_docker_stop_is_one_suspect_not_three_events():
    events = [{"ts": at(-2), "service": "pg", "container": "db", "action": "kill", "exit_code": None, "detail": "signal 15"},
              {"ts": at(-2) , "service": "pg", "container": "db", "action": "die", "exit_code": "0", "detail": ""},
              {"ts": at(-2), "service": "pg", "container": "db", "action": "stop", "exit_code": None, "detail": ""}]
    [s] = sus.from_container_events(events)
    assert s.kind == "stop" and "stopped on request" in s.what and "signal 15" in s.what and "exit code 0" in s.what
    crash = sus.from_container_events([{"ts": at(-1), "service": "api", "container": "api", "action": "die", "exit_code": "1"}])
    assert crash[0].kind == "crash" and "exited with code 1" in crash[0].what
    oom = sus.from_container_events([{"ts": at(-1), "service": "api", "container": "api", "action": "oom"},
                                     {"ts": at(-1), "service": "api", "container": "api", "action": "die", "exit_code": "137"}])
    assert oom[0].kind == "oom"


def test_timing_facts_for_each_kind_of_suspect():
    suspects = [sus.Suspect(T0 - 20, "orders", "deploy", "v1.8.0"),            # seconds before
                sus.Suspect(T0 - 1800, "gateway", "restart", "stack restart"),  # half an hour before, system clean since
                sus.Suspect(T0 + 90, "orders", "restart", "later restart")]     # after the problem began
    lines = sus.annotate(suspects, T0, failing_between=lambda a, b: False)
    text = "\n".join(lines)
    assert "S1" in lines[1] and "20 s before the problem began: immediately before" in lines[1]  # nearest first
    assert "S2" in lines[2] and "no check failed in the 30 minutes after it" in lines[2] and "delayed effect" in lines[2]
    assert "S3" in lines[3] and "AFTER the problem began" in text
    busy = sus.annotate(suspects[1:2], T0, failing_between=lambda a, b: True)
    assert "already failing in between" in busy[1]


def test_no_events_says_so_instead_of_inviting_a_guess():
    [line] = sus.annotate([], T0, failing_between=lambda a, b: False)
    assert "No change or container event" in line and "Do not name a cause the data does not show" in line


def test_old_events_outside_the_window_are_ignored():
    lines = sus.annotate([sus.Suspect(T0 - 3 * 3600, "x", "deploy", "ancient")], T0, lambda a, b: False)
    assert len(lines) == 1 and "No change or container event" in lines[0]


def _incident(suspect: str, confidence: float = 0.9) -> Incident:
    inc = Incident(anomalies=[])
    inc.suspect, inc.confidence = suspect, confidence
    return inc


def test_a_conclusion_resting_on_a_suspect_the_timing_argues_against_is_capped():
    timeline = ["  S1 06:19:40 deploy on orders: v1.8.0. 20 s before the problem began: immediately before.",
                "  S2 05:50:00 restart on gw: stack restart. 30 min before; no check failed in the 30 minutes after it, "
                "so only a delayed effect (a leak) could link it to this problem."]
    weak = _incident("S2")
    Investigator._cap_by_suspect(weak, timeline)
    assert weak.confidence == 0.5 and "does not fit the timing" in weak.reasoning[-1]
    strong = _incident("S1")
    Investigator._cap_by_suspect(strong, timeline)
    assert strong.confidence == 0.9
    for other in ("none", "", "the deploy"):
        free = _incident(other)
        Investigator._cap_by_suspect(free, timeline)
        assert free.confidence == 0.9
