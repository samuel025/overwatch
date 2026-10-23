"""Discovery: turn "where things are" into a complete, reviewable overwatch.yaml.

discover() runs whichever discoverers apply (Docker, Prometheus, SQL databases) and merges what they
find into one plan: services, topology, checks, catalog, evidence pack, service knowledge. render()
turns a plan into YAML plus the secrets that belong in .env. The CLI (`overwatch connect`) and the
console's setup page both use these two functions.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .docker import discover_docker
from .externals import merge_suggestions
from .prometheus import discover_prometheus
from .sql import discover_sql, sqlalchemy_url


def _env_name(source: str) -> str:
    return "OW_" + re.sub(r"[^A-Z0-9]", "_", source.upper()) + "_URL"


def discover(docker: dict[str, Any] | None = None, prometheus_url: str = "",
             databases: dict[str, str] | None = None) -> dict[str, Any]:
    """docker: {"project": ..., "service_label": ..., "socket": ...} or None to skip."""
    plan: dict[str, Any] = {
        "system": {"name": "my-system", "description": ""}, "docker": {}, "services": {}, "runtime_facts": "",
        "sources": {}, "catalog": [], "topology": {}, "checks": [], "evidence": [], "notes": [], "errors": [],
        "externals": [], "log_source": None,
    }
    env_hits: dict[str, dict[str, str]] = {}
    log_hits: dict[str, dict[str, int]] = {}
    internal: set[str] = set()

    if docker is not None:
        try:
            d = discover_docker(docker.get("project") or None, docker.get("service_label") or "com.docker.compose.service",
                                docker.get("socket") or "/var/run/docker.sock")
            if d.get("needs_project"):
                plan["errors"].append("Several Docker Compose projects are running; pick one: " + ", ".join(d["projects"]))
                plan["docker_projects"] = d["projects"]
            else:
                plan["system"]["name"] = d["project"]
                plan["docker"] = {"project": d["project"], "service_label": d["service_label"]}
                plan["docker_projects"] = d["projects"]
                plan["services"].update(d["services"])
                plan["topology"] = d["topology"]
                plan["checks"] += d["checks"]
                plan["runtime_facts"] = d["runtime_facts"]
                plan["notes"] += d["notes"]
                env_hits.update(d["env_hits"])
                for svc, hosts in d["log_hits"].items():
                    log_hits.setdefault(svc, {}).update(hosts)
                internal |= set(d["internal"])
                plan["log_source"] = {"type": "docker", "source": "docker"}
        except Exception as e:
            plan["errors"].append(f"Docker: {e}")

    if prometheus_url:
        try:
            p = discover_prometheus(prometheus_url)
            plan["sources"]["prom"] = {"type": "prometheus", "url": prometheus_url}
            plan["catalog"].append(p["catalog"])
            plan["checks"] += p["checks"]
            plan["notes"] += p["notes"]
            for svc in p["services"]:
                plan["services"].setdefault(svc, {"what": "TODO: describe what this service does"})
        except Exception as e:
            plan["errors"].append(f"Prometheus at {prometheus_url}: {e}")

    for name, url in (databases or {}).items():
        try:
            s = discover_sql(name, url)
            plan["sources"][name] = {"type": "postgres" if s["dialect"] == "postgresql" else "sql",
                                     "url": sqlalchemy_url(url)}
            plan["catalog"].append(s["catalog"])
            plan["checks"] += s["checks"]
            plan["evidence"] += s["evidence"]
            plan["notes"] += s["notes"]
            for svc in s["services"]:
                plan["services"].setdefault(svc, {"what": "TODO: describe what this service does"})
            for svc, hosts in s["log_hits"].items():
                for h, n in hosts.items():
                    log_hits.setdefault(svc, {})[h] = log_hits.get(svc, {}).get(h, 0) + n
            if s.get("logs") and (plan["log_source"] or {}).get("type") != "sql":
                plan["log_source"] = {"type": "sql", "source": name, "dialect": s["dialect"], **s["logs"]}
        except Exception as e:
            plan["errors"].append(f"Database {name}: {str(e).splitlines()[0]}")
    internal |= set(plan["services"]) | {h for src in plan["sources"].values() for h in re.findall(r"@([^:/]+)", src.get("url", ""))}
    plan["externals"] = merge_suggestions(env_hits, log_hits, {h.lower() for h in internal})

    seen, unique = set(), []
    for c in plan["checks"]:
        if c["name"] not in seen:
            seen.add(c["name"])
            unique.append({**c, "enabled": True})
    plan["checks"] = unique
    plan["evidence"] = [{**e, "enabled": True} for e in plan["evidence"]]
    plan["catalog"] = "\n".join(plan["catalog"])
    services = sorted(plan["services"])
    plan["system"]["description"] = (
        f"{len(services)} services: {', '.join(services)}. " if services else ""
    ) + "TODO: one or two sentences on what the system does and how requests flow."
    return plan


class _Literal(str):
    pass


yaml.SafeDumper.add_representer(_Literal, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style="|"))


def _lit(text: str) -> Any:
    return _Literal(text.rstrip() + "\n") if "\n" in text.strip() else text


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _apply_externals(plan: dict[str, Any]) -> tuple[dict[str, list[str]], dict[str, Any], list[dict[str, Any]]]:
    """Accepted external services become topology edges, service knowledge, and checks."""
    topology = {k: list(v) for k, v in plan.get("topology", {}).items()}
    services = {k: dict(v) for k, v in plan.get("services", {}).items()}
    checks: list[dict[str, Any]] = []
    log_source = plan.get("log_source") or {}
    for ext in plan.get("externals", []):
        name = str(ext.get("name", "")).strip()
        if not ext.get("enabled", True) or not name:
            continue
        for caller in ext.get("callers", []):
            deps = topology.setdefault(caller, [])
            if name not in deps:
                deps.append(name)
        services[name] = {
            "what": ext.get("what") or "external dependency (third party); we only see it through our services",
            "called_by": ", ".join(ext.get("callers", [])) or "unknown",
        }
        if url := str(ext.get("health_url", "")).strip():
            checks.append({"name": f"{_key(name)}_up", "kind": "probe", "service": name, "url": url,
                           "timeout_ms": 3000, "severity": "high", "description": f"{name} answered its health/status URL"})
        text = str(ext.get("log_text", "")).strip().replace("'", "")
        if text and log_source.get("type") == "sql":
            pg = log_source.get("dialect") == "postgresql"
            window = "now() - interval '2 minutes'" if pg else "datetime('now', '-2 minutes')"
            checks.append({
                "name": f"{_key(name)}_errors", "service": name, "source": log_source["source"],
                "query": _lit(f"SELECT count(*) AS value FROM {log_source['table']}\n"
                              f"WHERE upper({log_source['level']}) IN ('ERROR', 'FATAL', 'CRITICAL')\n"
                              f"  AND message LIKE '%{text}%' AND {log_source['ts']} > {window}"),
                "threshold": 5, "severity": "high", "description": f"errors mentioning {name}, last 2 minutes",
            })
        elif text and log_source.get("type") == "docker":
            checks.append({
                "name": f"{_key(name)}_errors", "service": name, "source": log_source["source"],
                "query": f"count {','.join(ext.get('callers') or ['*'])} 2 {text}", "threshold": 5, "severity": "high",
                "description": f"error log lines mentioning {name}, last 2 minutes",
            })
    return topology, services, checks


def render(plan: dict[str, Any], llm: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """YAML text for overwatch.yaml, and the secrets (env var -> value) that belong in .env."""
    env: dict[str, str] = {}
    sources = {}
    for name, src in plan["sources"].items():
        if src.get("type") in ("postgres", "sql", "zetaris"):
            var = _env_name(name)
            env[var] = src["url"]
            sources[name] = {**src, "url": f"${{{var}}}"}
        else:
            sources[name] = src

    llm = llm or {}
    if llm.get("api_key"):
        env["LLM_API_KEY"] = llm["api_key"]
    if llm.get("base_url"):
        env["LLM_BASE_URL"] = llm["base_url"]
    if llm.get("model"):
        env["OVERWATCH_MODEL"] = llm["model"]
    if llm.get("triage_model"):
        env["OVERWATCH_TRIAGE_MODEL"] = llm["triage_model"]

    strip = lambda d: {k: v for k, v in d.items() if k not in ("enabled", "why")}  # noqa: E731
    topology, services, extra_checks = _apply_externals(plan)
    checks = [strip(c) for c in plan["checks"] + extra_checks if c.get("enabled", True)]
    for c in checks:
        if "query" in c:
            c["query"] = _lit(c["query"])
    evidence = [strip(e) for e in plan["evidence"] if e.get("enabled", True)]
    doc: dict[str, Any] = {
        "system": {"name": plan["system"]["name"], "description": plan["system"]["description"]},
    }
    if plan.get("docker"):
        doc["docker"] = plan["docker"]
    doc["services"] = services
    if plan.get("runtime_facts"):
        doc["runtime_facts"] = _lit(plan["runtime_facts"])
    if sources:
        doc["sources"] = sources
    if plan.get("catalog"):
        doc["catalog"] = _lit(plan["catalog"])
    if topology:
        doc["topology"] = topology
    if evidence:
        doc["evidence"] = evidence
    doc["checks"] = checks
    doc["watch"] = {"interval_seconds": 10, "confirm_samples": 2}
    doc["llm"] = {
        "base_url": "${LLM_BASE_URL:-http://127.0.0.1:8088/v1}",
        "api_key": "${LLM_API_KEY}",
        "investigator_model": "${OVERWATCH_MODEL:-meterless/text.deep}",
        "triage_model": "${OVERWATCH_TRIAGE_MODEL:-meterless/text.fast}",
        "max_steps": 8,
    }
    doc["memory"] = {"type": "hmem", "url": "${MEMORY_URL:-http://127.0.0.1:8099}"}
    doc["approval"] = "manual"
    doc["notifiers"] = [{"type": "console"}]

    header = (
        f"# Generated by Overwatch discovery on {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}.\n"
        "# Review the TODOs (they help the Investigator most), then: overwatch -c overwatch.yaml run\n"
        "# Secrets are in .env, referenced here as ${VAR}.\n"
    )
    body = yaml.safe_dump(doc, sort_keys=False, width=110, allow_unicode=True, default_flow_style=False)
    return header + body, env


def write(plan: dict[str, Any], config_path: str | Path, llm: dict[str, str] | None = None) -> dict[str, Any]:
    """Write overwatch.yaml (backing up an existing one) and merge secrets into .env next to it."""
    text, env = render(plan, llm)
    path = Path(config_path)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
        path.rename(backup)
    path.write_text(text)
    env_path = path.parent / ".env"
    existing = env_path.read_text().splitlines() if env_path.exists() else []
    keep = [line for line in existing if line.split("=", 1)[0].strip() not in env]
    env_path.write_text("\n".join(keep + [f"{k}={v}" for k, v in env.items()]).strip() + "\n")
    env_path.chmod(0o600)
    return {"config": str(path), "backup": str(backup) if backup else None, "env": str(env_path),
            "secrets": sorted(env)}
