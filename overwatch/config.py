"""Load overwatch.yaml, expanding ${ENV_VAR} references so secrets stay in the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_dotenv(path: str | Path = ".env") -> None:
    """Minimal .env loader; existing environment variables win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _read_token_file(path: str) -> str:
    """Nightshift writes its access token to <stateDir>/proxy-token; allow pointing at it directly."""
    p = Path(path).expanduser() if path else None
    return p.read_text().strip() if p and p.is_file() else ""


@dataclass
class CheckConfig:
    name: str
    source: str = ""
    query: str = ""
    threshold: float | None = None  # fixed limit; optional when relative_factor is set
    service: str = "unknown"
    comparison: str = ">"  # one of > >= < <=
    description: str = ""
    severity: str = "medium"
    no_data: str = "ignore"  # "alert": a check that returns no value is itself an anomaly (service went silent)
    kind: str = "metric"  # "heartbeat" / "probe": when it fires, the service is shown as not responding
    url: str = ""  # probe: the health URL Overwatch calls itself (value 1 = answered 2xx, 0 = did not)
    timeout_ms: int = 2000  # probe timeout
    relative_factor: float | None = None  # alert when value > factor x this check's own recent median
    relative_min: float = 0.0  # ...and the value is at least this (ignores noise on tiny baselines)
    baseline_minutes: int = 30


@dataclass
class ImpactConfig:
    """A business-impact number for the blast radius, e.g. failed checkouts since the incident began."""

    name: str
    label: str
    source: str
    query: str  # may use {since}: the incident start as a timestamp literal
    unit: str = ""


@dataclass
class EvidenceConfig:
    """A query run automatically, in parallel, the moment an incident opens (the evidence pack)."""

    name: str
    label: str
    source: str
    query: str  # may use {since}: a timestamp literal shortly before the incident started


@dataclass
class LLMConfig:
    base_url: str
    api_key: str
    investigator_model: str
    summary_model: str
    temperature: float = 0.1
    max_steps: int = 8
    triage_model: str = ""  # optional fast model for a first guess while the deep investigation runs
    triage_extra_body: dict[str, Any] = field(default_factory=dict)  # provider options, e.g. turn off reasoning
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Config:
    system_name: str
    system_description: str
    sources: dict[str, dict[str, Any]]
    catalog: str
    checks: list[CheckConfig]
    llm: LLMConfig
    services: dict[str, dict[str, Any]] = field(default_factory=dict)  # what an engineer would tell a newcomer
    runtime_facts: str = ""  # how the platform behaves: exit codes, restart policy, what is NOT used
    memory: dict[str, Any] = field(default_factory=dict)
    notifiers: list[dict[str, Any]] = field(default_factory=list)
    approval: str = "manual"
    topology: dict[str, list[str]] = field(default_factory=dict)  # service -> services it calls
    impacts: list[ImpactConfig] = field(default_factory=list)
    evidence: list[EvidenceConfig] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)  # pushed event streams, e.g. docker
    interval_seconds: int = 30
    confirm_samples: int = 2  # a check must breach this many passes in a row to open an incident
    event_grace_seconds: float = 8  # a container back up within this is a restart, not an outage
    cooldown_seconds: int = 600
    merge_window_seconds: int = 300  # new alerts join an incident opened this recently instead of a new one
    resolve_after_seconds: int = 60  # an incident recovers once its checks stay healthy this long
    max_parallel_investigations: int = 2
    trace_dir: str = "traces"
    state_path: str = ""  # SQLite file for incidents; default <trace_dir>/overwatch.db
    console_token: str = ""  # required by the web console/API when set


def load_config(path: str | Path) -> Config:
    path = Path(path)
    load_dotenv()  # current directory first; then the nearest .env at or above the config file
    for folder in [path.resolve().parent, *path.resolve().parent.parents]:
        if (folder / ".env").exists():
            load_dotenv(folder / ".env")
            break
    raw = _expand(yaml.safe_load(path.read_text()))

    system = raw.get("system", {})
    llm = raw.get("llm", {})
    watch = raw.get("watch", {})
    checks = [CheckConfig(**c) for c in raw.get("checks", [])]

    sources = raw.get("sources", {})
    for c in checks:
        if c.kind == "probe":
            if not c.url:
                raise ValueError(f"probe '{c.name}' needs a url")
            if c.threshold is None:  # a probe reports 1 (answered) or 0 (did not)
                c.threshold, c.comparison = 1, "<"
        elif c.source not in sources:
            raise ValueError(f"check '{c.name}' uses unknown source '{c.source}'")
        if c.comparison not in (">", ">=", "<", "<="):
            raise ValueError(f"check '{c.name}': comparison must be one of > >= < <=")
        if c.threshold is None and not c.relative_factor:
            raise ValueError(f"check '{c.name}' needs a threshold, a relative_factor, or both")
        if c.kind not in ("metric", "heartbeat", "probe"):
            raise ValueError(f"check '{c.name}': kind must be 'metric', 'heartbeat' or 'probe'")
        if c.no_data not in ("ignore", "alert"):
            raise ValueError(f"check '{c.name}': no_data must be 'ignore' or 'alert'")

    impacts = [ImpactConfig(**i) for i in raw.get("impact", [])]
    for i in impacts:
        if i.source not in sources:
            raise ValueError(f"impact '{i.name}' uses unknown source '{i.source}'")
    evidence = [EvidenceConfig(**e) for e in raw.get("evidence", [])]
    for e in evidence:
        if e.source not in sources:
            raise ValueError(f"evidence '{e.name}' uses unknown source '{e.source}'")
    topology = {str(k): [str(d) for d in (v or [])] for k, v in (raw.get("topology") or {}).items()}

    approval = raw.get("approval", "manual")
    if approval not in ("manual", "auto"):
        raise ValueError("approval must be 'manual' or 'auto'")

    return Config(
        system_name=system.get("name", "my-system"),
        system_description=system.get("description", ""),
        services={str(k): dict(v or {}) for k, v in (raw.get("services") or {}).items()},
        runtime_facts=raw.get("runtime_facts", ""),
        sources=sources,
        catalog=raw.get("catalog", ""),
        checks=checks,
        llm=LLMConfig(
            base_url=llm.get("base_url", ""),
            api_key=llm.get("api_key", "") or _read_token_file(llm.get("token_file", "")),
            investigator_model=llm.get("investigator_model", ""),
            summary_model=llm.get("summary_model", llm.get("investigator_model", "")),
            temperature=float(llm.get("temperature", 0.1)),
            max_steps=int(llm.get("max_steps", 8)),
            triage_model=llm.get("triage_model", ""),
            triage_extra_body=llm.get("triage_extra_body") or {},
            headers={str(k): str(v) for k, v in (llm.get("headers") or {}).items()},
        ),
        memory=raw.get("memory", {"type": "hmem", "url": "http://127.0.0.1:8099"}),
        notifiers=raw.get("notifiers", [{"type": "console"}]),
        approval=approval,
        topology=topology,
        impacts=impacts,
        evidence=evidence,
        events=raw.get("events", []),
        confirm_samples=max(1, int(watch.get("confirm_samples", 2))),
        event_grace_seconds=float(watch.get("event_grace_seconds", 8)),
        interval_seconds=int(watch.get("interval_seconds", 30)),
        cooldown_seconds=int(watch.get("cooldown_seconds", 600)),
        merge_window_seconds=int(watch.get("merge_window_seconds", 300)),
        resolve_after_seconds=int(watch.get("resolve_after_seconds", 60)),
        max_parallel_investigations=int(watch.get("max_parallel_investigations", 2)),
        trace_dir=raw.get("trace_dir", "traces"),
        state_path=raw.get("state_path", ""),
        console_token=(raw.get("console") or {}).get("token", "") or os.environ.get("OVERWATCH_TOKEN", ""),
    )
