"""Edit a running configuration from the console, without re-running discovery.

read() turns overwatch.yaml into the sections the settings page edits (system, services, external services,
checks, AI provider, notifications, watching). save() applies the edited sections to the raw YAML, keeps
everything it does not know about, backs the old file up, and puts secrets in .env, never in the YAML.

Values written as ${VAR} or ${VAR:-default} in the YAML stay that way: the new value goes to .env under VAR.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import _ENV_REF, load_dotenv

_WHOLE_REF = re.compile(r"^\$\{([A-Z0-9_]+)(?::-([^}]*))?\}$")

NOTIFIER_FIELDS: dict[str, dict[str, str]] = {  # field -> env var used when the field is a secret
    "slack": {"webhook_url": "SLACK_WEBHOOK_URL"},
    "jira": {"base_url": "", "email": "", "api_token": "JIRA_API_TOKEN", "project_key": "", "issue_type": ""},
    "webhook": {"url": "OW_WEBHOOK_URL"},
}
SECRET = {"webhook_url", "api_token", "api_key", "url"}


class _Literal(str):
    pass


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(_Literal, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|"))


def _blocks(value: Any) -> Any:
    """Multi-line strings (queries, descriptions) stay readable block scalars."""
    if isinstance(value, str) and "\n" in value.strip():
        return _Literal(value.rstrip() + "\n")
    if isinstance(value, list):
        return [_blocks(v) for v in value]
    if isinstance(value, dict):
        return {k: _blocks(v) for k, v in value.items()}
    return value


def _resolve(value: Any) -> str:
    if not isinstance(value, str):
        return "" if value is None else str(value)
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _is_external(name: str, facts: dict[str, Any]) -> bool:
    return str(facts.get("external", "")).lower() in ("true", "yes") or "external" in str(facts.get("what", "")).lower()


def _log_text(check: dict[str, Any]) -> str:
    query = str(check.get("query", ""))
    if m := re.search(r"LIKE '%([^%']+)%'", query):
        return m.group(1)
    if m := re.match(r"count \S+ \d+ (.+)", query.strip()):
        return m.group(1)
    return ""


def _load_env(path: Path) -> None:
    for folder in [path.resolve().parent, *path.resolve().parent.parents]:
        if (folder / ".env").exists():
            load_dotenv(folder / ".env")
            return


def read(config_path: str | Path) -> dict[str, Any]:
    path = Path(config_path)
    _load_env(path)
    raw = yaml.safe_load(path.read_text()) or {}
    services = raw.get("services") or {}
    topology = raw.get("topology") or {}
    checks = raw.get("checks") or []

    externals, internal = [], []
    for name, facts in services.items():
        facts = facts or {}
        if _is_external(name, facts):
            probe, errors = _external_checks(name, checks)
            externals.append({
                "name": name, "original": name, "what": str(facts.get("what", "")),
                "callers": sorted(k for k, deps in topology.items() if name in (deps or [])),
                "health_url": str((probe or {}).get("url", "")), "log_text": _log_text(errors or {}),
                "enabled": True, "suggested": False, "evidence": [],
            })
        else:
            internal.append({"name": name, "what": str(facts.get("what", "")), "normal": str(facts.get("normal", "")),
                             "runs_on": str(facts.get("runs_on", ""))})
    for name in sorted({d for deps in topology.values() for d in (deps or [])} | set(topology)):
        if name not in services:
            internal.append({"name": name, "what": "", "normal": "", "runs_on": ""})

    llm = raw.get("llm") or {}
    watch = raw.get("watch") or {}
    notifiers: dict[str, dict[str, Any]] = {}
    for n in raw.get("notifiers") or []:
        kind = n.get("type")
        if kind not in NOTIFIER_FIELDS:
            continue
        entry: dict[str, Any] = {"enabled": bool(n.get("enabled", True))}
        for field in NOTIFIER_FIELDS[kind]:
            value = _resolve(n.get(field, ""))
            if field in SECRET:
                entry[field] = ""
                entry[f"{field}_set"] = bool(value)
            else:
                entry[field] = value
        notifiers[kind] = entry

    return {
        "config_path": str(path.resolve()),
        "system": {"name": str((raw.get("system") or {}).get("name", "")),
                   "description": str((raw.get("system") or {}).get("description", "")).strip()},
        "services": internal,
        "externals": externals,
        "service_names": sorted({s["name"] for s in internal}),
        "checks": [{
            "name": c.get("name"), "kind": c.get("kind", "metric"), "service": c.get("service", ""),
            "description": c.get("description", ""), "severity": c.get("severity", "medium"),
            "threshold": c.get("threshold"), "comparison": c.get("comparison", ">"),
            "relative_factor": c.get("relative_factor"), "enabled": c.get("enabled", True) is not False,
        } for c in checks],
        "sources": [{"name": k, "type": (v or {}).get("type", "")} for k, v in (raw.get("sources") or {}).items()],
        "ai": {
            "base_url": _resolve(llm.get("base_url", "")), "model": _resolve(llm.get("investigator_model", "")),
            "triage_model": _resolve(llm.get("triage_model", "")), "api_key": "",
            "api_key_set": bool(_resolve(llm.get("api_key", ""))), "max_steps": int(llm.get("max_steps", 8)),
        },
        "notifiers": notifiers,
        "watch": {
            "interval_seconds": int(watch.get("interval_seconds", 30)),
            "confirm_samples": int(watch.get("confirm_samples", 2)),
            "cooldown_seconds": int(watch.get("cooldown_seconds", 600)),
            "merge_window_seconds": int(watch.get("merge_window_seconds", 300)),
            "resolve_after_seconds": int(watch.get("resolve_after_seconds", 60)),
            "max_parallel_investigations": int(watch.get("max_parallel_investigations", 2)),
        },
        "approval": raw.get("approval", "manual"),
    }


def _assign(current: Any, new: str, env: dict[str, str], secret_var: str = "") -> Any:
    """Keep ${VAR} references (the value goes to .env); move new secrets out of the YAML."""
    if isinstance(current, str) and (m := _WHOLE_REF.match(current.strip())):
        env[m.group(1)] = new
        return current
    if secret_var:
        env[secret_var] = new
        return f"${{{secret_var}}}"
    return new


def save(config_path: str | Path, edits: dict[str, Any]) -> dict[str, Any]:
    path = Path(config_path)
    raw = yaml.safe_load(path.read_text()) or {}
    env: dict[str, str] = {}

    if sys_ := edits.get("system"):
        raw.setdefault("system", {}).update({"name": sys_["name"], "description": sys_["description"]})

    services = dict(raw.get("services") or {})
    topology = {k: list(v or []) for k, v in (raw.get("topology") or {}).items()}
    checks = list(raw.get("checks") or [])

    for s in edits.get("services", []):
        if s["name"] not in services and not s.get("what"):
            continue
        facts = dict(services.get(s["name"]) or {})
        for field in ("what", "normal"):
            if s.get(field):
                facts[field] = s[field]
            else:
                facts.pop(field, None)
        services[s["name"]] = facts

    if "externals" in edits:
        _apply_externals(raw, services, topology, checks, edits["externals"])

    by_name = {c["name"]: c for c in edits.get("checks", [])}
    for c in checks:
        if (e := by_name.get(c.get("name"))) is None:
            continue
        if e.get("enabled", True):
            c.pop("enabled", None)
        else:
            c["enabled"] = False
        if e.get("severity"):
            c["severity"] = e["severity"]
        if e.get("threshold") not in (None, "") and c.get("kind", "metric") not in ("probe", "container"):
            c["threshold"] = float(e["threshold"])

    raw["services"], raw["checks"] = services, checks
    if topology:
        raw["topology"] = {k: v for k, v in topology.items() if v}

    if ai := edits.get("ai"):
        llm = raw.setdefault("llm", {})
        for field, key, secret in (("base_url", "base_url", ""), ("model", "investigator_model", ""),
                                   ("triage_model", "triage_model", ""), ("api_key", "api_key", "LLM_API_KEY")):
            if ai.get(field):
                llm[key] = _assign(llm.get(key), ai[field], env, secret)
        if ai.get("max_steps"):
            llm["max_steps"] = int(ai["max_steps"])

    if (notifiers := edits.get("notifiers")) is not None:
        existing = {n.get("type"): n for n in raw.get("notifiers") or []}
        out = [existing.get("console", {"type": "console"})]
        for kind, fields in NOTIFIER_FIELDS.items():
            entry = dict(existing.get(kind) or {"type": kind})
            new = notifiers.get(kind) or {}
            if kind not in existing and not new.get("enabled"):
                continue
            entry["enabled"] = bool(new.get("enabled"))
            for field, var in fields.items():
                if new.get(field):
                    entry[field] = _assign(entry.get(field), str(new[field]), env, var if field in SECRET else "")
            out.append(entry)
        raw["notifiers"] = out + [n for t, n in existing.items() if t not in NOTIFIER_FIELDS and t != "console"]

    if watch := edits.get("watch"):
        raw.setdefault("watch", {}).update({k: int(v) for k, v in watch.items() if v not in (None, "")})
    if edits.get("approval") in ("manual", "auto"):
        raw["approval"] = edits["approval"]

    backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
    text = path.read_text()
    backup.write_text(text)
    lead = []
    for line in text.splitlines():  # keep the file's opening comment block
        if not line.startswith("#"):
            break
        if not line.startswith("# Last edited in the Overwatch console"):
            lead.append(line + "\n")
    header = "".join(lead) + f"# Last edited in the Overwatch console on {datetime.now():%Y-%m-%d %H:%M}. Previous: {backup.name}\n"
    path.write_text(header + yaml.dump(_blocks(raw), Dumper=_Dumper, sort_keys=False, width=110,
                                       allow_unicode=True, default_flow_style=False))
    if env:
        _write_env(path.parent / ".env", env)
        os.environ.update(env)
    return {"config": str(path), "backup": str(backup), "secrets": sorted(env)}


def _write_env(env_path: Path, env: dict[str, str]) -> None:
    existing = env_path.read_text().splitlines() if env_path.exists() else []
    keep = [line for line in existing if line.split("=", 1)[0].strip() not in env]
    env_path.write_text("\n".join(keep + [f"{k}={v}" for k, v in env.items()]).strip() + "\n")
    env_path.chmod(0o600)


def _external_checks(name: str, checks: list[dict[str, Any]]) -> tuple[dict | None, dict | None]:
    """The probe and the log-error check that watch an external service (hand-written or generated)."""
    probe = next((c for c in checks if c.get("service") == name and c.get("kind") == "probe"), None)
    errors = next((c for c in checks if c.get("service") == name and c.get("kind", "metric") == "metric"
                   and _log_text(c)), None)
    return probe, errors


def _apply_externals(raw: dict[str, Any], services: dict[str, Any], topology: dict[str, list[str]],
                     checks: list[dict[str, Any]], edited: list[dict[str, Any]]) -> None:
    """Sync external services: service entry, topology edges, and their probe / log-error checks.

    Existing checks are updated in place, so hand-written ones keep their names, windows and thresholds.
    """
    before = {n: dict(f or {}) for n, f in services.items() if _is_external(n, f or {})}
    kept = {str(e.get("original") or "") for e in edited if e.get("enabled", True) and str(e.get("name", "")).strip()}
    for name in before:
        if name not in kept:  # removed or switched off: forget it entirely
            services.pop(name, None)
            checks[:] = [c for c in checks if c.get("service") != name]
        for deps in topology.values():
            if name in deps:
                deps.remove(name)
    template = next((c for c in checks if c.get("kind", "metric") == "metric" and _log_text(c)), None)

    for ext in edited:
        name = str(ext.get("name", "")).strip()
        if not name or not ext.get("enabled", True):
            continue
        original = str(ext.get("original") or "")
        facts = before.get(original, {})
        if original and original != name:  # renamed
            services.pop(original, None)
            for c in checks:
                if c.get("service") == original:
                    c["service"] = name
        facts["what"] = ext.get("what") or facts.get("what") or \
            "external dependency (third party); we only see it through our services"
        if "external" not in facts["what"].lower():
            facts["external"] = "yes"
        services[name] = facts
        callers = [c for c in ext.get("callers", []) if c != name]
        for caller in callers:
            deps = topology.setdefault(caller, [])
            if name not in deps:
                deps.append(name)

        key = _key(name)
        probe, errors = _external_checks(name, checks)
        url = str(ext.get("health_url", "")).strip()
        if probe and url:
            probe["url"] = url
        elif probe:
            checks.remove(probe)
        elif url:
            checks.append({"name": f"{key}_up", "kind": "probe", "service": name, "url": url, "timeout_ms": 3000,
                           "severity": "high", "description": f"{name} answered its health/status URL"})

        text = str(ext.get("log_text", "")).strip().replace("'", "").replace("%", "")
        if errors and text:
            errors["query"] = str(errors["query"]).replace(_log_text(errors), text)
        elif errors:
            checks.remove(errors)
        elif text:
            if raw.get("docker") or "docker" in (raw.get("sources") or {}):
                checks.append({"name": f"{key}_errors", "service": name, "source": "docker",
                               "query": f"count {','.join(callers) or '*'} 2 {text}", "threshold": 5,
                               "severity": "high", "description": f"error log lines mentioning {name}, last 2 minutes"})
            elif template:  # same log table and window as an existing log-error check
                checks.append({**template, "name": f"{key}_errors", "service": name,
                               "query": str(template["query"]).replace(_log_text(template), text),
                               "description": f"errors mentioning {name} in the logs"})
