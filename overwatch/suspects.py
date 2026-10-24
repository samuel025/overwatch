"""Ranked suspects: what changed or restarted shortly before a problem began, and how the timing fits.

A model asked "what caused this?" will happily blame the nearest event, even one that happened half an hour earlier
with a clean system in between, or one that happened after the problem began. Overwatch already knows when the
problem began (the first failing sample) and keeps every check's history, so it computes the facts the model would
otherwise guess: each change and container event in the hour before, how long before, and whether anything was
failing in between. The Investigator reads this list and names the suspect its conclusion rests on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

WINDOW_SECONDS = 3600
IMMEDIATE_SECONDS = 120   # a cause usually acts within this long of the problem starting
MAX_SUSPECTS = 8


@dataclass
class Suspect:
    ts: float
    service: str
    kind: str   # deploy, config, restart, stop, oom, ...
    what: str


def parse_ts(value: Any) -> float | None:
    """ISO-8601 (with 'T' or a space, with or without a zone; naive means UTC) to a unix time."""
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or len(value) < 16:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace(" ", "T", 1).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


_TS = ("ts", "created_at", "deployed_at", "timestamp", "time", "date", "released_at", "updated_at")
_SERVICE = ("service", "service_name", "app", "application", "component", "project", "repo")
_VERSION = ("version", "tag", "release", "ref", "sha", "commit")
_WHAT = ("description", "message", "title", "summary", "notes", "details", "commit_message", "body")


def _pick(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    lowered = {str(k).lower(): v for k, v in row.items()}
    return next((lowered[n] for n in names if lowered.get(n) not in (None, "")), None)


def from_changes(rows: list[dict[str, Any]]) -> list[Suspect]:
    """Rows from a change log. Column names vary between systems, so the usual ones are recognised: a time, a service,
    a version and a description. A row with no recognisable time is skipped."""
    out = []
    for r in rows:
        ts = parse_ts(_pick(r, _TS))
        if ts is None:  # a differently named time column: take the first value that reads as a timestamp
            ts = next((t for t in (parse_ts(v) for v in r.values() if isinstance(v, str)) if t is not None), None)
        if ts is None:
            continue
        version = str(_pick(r, _VERSION) or "").strip()
        desc = str(_pick(r, _WHAT) or "").strip()
        kind = str(r.get("kind") or ("config" if version.startswith("config") else "deploy"))
        out.append(Suspect(ts, str(_pick(r, _SERVICE) or "?"), kind,
                           " ".join(p for p in (version, f"'{desc[:90]}'" if desc else "") if p) or "change"))
    return out


def from_container_events(rows: list[dict[str, Any]], cluster_seconds: float = 20) -> list[Suspect]:
    """Container events (kill, die, stop, start, oom...) grouped per container: one stop is one suspect, not three."""
    by_container: dict[str, list[tuple[float, dict[str, Any]]]] = {}
    for r in rows:
        ts = parse_ts(r.get("ts"))
        if ts is not None:
            by_container.setdefault(str(r.get("container") or r.get("service") or "?"), []).append((ts, r))
    out = []
    for container, events in by_container.items():
        events.sort(key=lambda e: e[0])
        group: list[tuple[float, dict[str, Any]]] = []
        for ev in events + [(float("inf"), {})]:
            if group and ev[0] - group[-1][0] > cluster_seconds:
                out.append(_describe(container, group))
                group = []
            if ev[1]:
                group.append(ev)
    return out


def _describe(container: str, group: list[tuple[float, dict[str, Any]]]) -> Suspect:
    actions = [str(r.get("action", "")).split(":")[0] for _, r in group]
    detail = next((str(r.get("detail") or "") for _, r in group if r.get("detail")), "")
    code = next((r.get("exit_code") for _, r in group if r.get("exit_code") not in (None, "")), None)
    service = next((str(r.get("service")) for _, r in group if r.get("service")), container)
    if "oom" in actions:
        kind, what = "oom", f"container {container} was killed for using too much memory"
    elif "kill" in actions or "stop" in actions:
        kind = "stop"
        what = f"container {container} was stopped on request" + (f" ({detail})" if detail else "") + \
               (f", exit code {code}" if code is not None else "")
        if "start" in actions and actions.index("start") > min(i for i, a in enumerate(actions) if a in ("kill", "stop")):
            kind, what = "restart", what + ", then started again"
    elif "die" in actions:
        kind = "crash"
        what = f"container {container} exited" + (f" with code {code}" if code is not None else "") + \
               (", then started again" if "start" in actions else "")
    else:
        kind, what = "start", f"container {container} started"
    return Suspect(group[0][0], service, kind, what)


def annotate(suspects: list[Suspect], started_at: float, failing_between: Callable[[float, float], bool],
             starts: dict[str, float] | None = None) -> list[str]:
    """Lines for the incident timeline, the suspect closest to a failure first. `starts` maps each failing check to the
    time it began failing; each suspect is described against those, because an incident can hold two problems and an
    event that came after the first check began failing may be exactly what started another. `failing_between(a, b)`
    says whether any check was failing between two unix times."""
    def clock(t: float) -> str:
        return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M:%S")

    starts = starts or {"the failing checks": started_at}
    seen, unique = set(), []
    for s in sorted(suspects, key=lambda s: s.ts):
        key = (round(s.ts), s.service, s.what[:40])
        if key not in seen and started_at - WINDOW_SECONDS <= s.ts:
            seen.add(key)
            unique.append(s)
    if not unique:
        return ["- No change or container event was recorded in the hour before the problem began, so the trigger may be "
                "external, gradual or not tracked. Do not name a cause the data does not show."]

    def following(s: Suspect) -> list[tuple[float, str]]:
        return sorted((t0 - s.ts, name) for name, t0 in starts.items() if t0 >= s.ts - 15)

    def rank(s: Suspect) -> tuple[int, float]:
        nxt = following(s)
        return (0, nxt[0][0]) if nxt else (1, -s.ts)  # nearest to a failure first; events after every failure began last

    ranked = sorted(unique, key=rank)[:MAX_SUSPECTS]
    begins = ", ".join(f"{name} {clock(t0)}" for name, t0 in sorted(starts.items(), key=lambda kv: kv[1])[:6])
    lines = [f"- Suspect events (computed from the change log and container events). Checks began failing at: {begins} UTC."]
    for i, s in enumerate(ranked, 1):
        nxt = following(s)
        if nxt:
            gap, name = nxt[0]
            names = ", ".join(n for g, n in nxt if g - gap <= 60)[:120]
            before = [n for n, t0 in starts.items() if t0 < s.ts - 15]
            earlier = f" ({', '.join(before[:3])} had already been failing)" if before else ""
            if gap <= IMMEDIATE_SECONDS:
                note = f"{max(0, round(gap))} s before {names} began failing: immediately before{earlier}."
            elif failing_between(s.ts + 45, s.ts + gap - 20):
                note = f"{gap / 60:.0f} min before {names} began failing; other checks were already failing in between{earlier}."
            else:
                note = (f"{gap / 60:.0f} min before {names} began failing; no check failed in the {gap / 60:.0f} minutes after it, "
                        f"so only a delayed effect (a leak, a cache expiring, a scheduled job) could link it{earlier}.")
        else:
            note = (f"every failing check had already begun failing before it ({', '.join(list(starts)[:3])}), so it did not start "
                    "those; it can only belong to a separate problem or be a response to them.")
        lines.append(f"  S{i} {clock(s.ts)} {s.kind} on {s.service}: {s.what}. {note}")
    return lines
