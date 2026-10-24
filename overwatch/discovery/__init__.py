"""Discovery: turn "where things are" into a complete, reviewable overwatch.yaml.

discover() runs whichever discoverers apply (Docker, Prometheus, SQL databases) and merges what they
find into one plan: services, topology, checks, catalog, evidence pack, service knowledge. render()
turns a plan into YAML plus the secrets that belong in .env. The CLI (`overwatch connect`) and the
console's setup page both use these two functions.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

import yaml

from .. import knowledge
from ..datalayer import source_config
from .docker import discover_docker
from .kubernetes import discover_kubernetes
from .loki import discover_loki
from .externals import merge_suggestions
from .prometheus import discover_prometheus
from .sql import discover_sql, sqlalchemy_url


def _env_name(source: str) -> str:
    return "OW_" + re.sub(r"[^A-Z0-9]", "_", source.upper()) + "_URL"


def match_service(name: str, known: set[str]) -> str | None:
    """Prometheus calls the API "url_shortener_api" and Docker Compose calls it "api". Map a monitoring name to the
    service it scrapes when its last words are exactly that service's name and no other service fits; otherwise None."""
    words = lambda text: [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]  # noqa: E731
    mine = words(name)
    hits = [k for k in known if words(k) and len(mine) > len(words(k)) and mine[-len(words(k)):] == words(k)]
    return hits[0] if len(hits) == 1 else None


def _database_service(url: str, services: dict[str, Any]) -> str | None:
    """The discovered service a database URL points at: the one publishing that port, or named like the host."""
    try:
        u = make_url(url)
    except Exception:
        return None
    for name, facts in services.items():
        if not isinstance(facts, dict):
            continue
        if name == u.host or any(p.get("PublicPort") == u.port for p in facts.get("ports") or []):
            return name
    return None


def _connections_check(name: str, source: str, service: str) -> dict[str, Any]:
    """Early warning: a database whose connection slots are nearly used up still answers, until the app is refused."""
    return {
        "name": f"{name}_connections_used_pct", "source": source, "service": service, "threshold": 85,
        "comparison": ">", "severity": "high",
        "query": "SELECT round(100.0 * count(*) / current_setting('max_connections')::int, 1) AS value FROM pg_stat_activity",
        "description": f"share of {name}'s connection slots in use (the app is refused when they run out)",
    }


def discover(docker: dict[str, Any] | None = None, prometheus_url: str = "",
             databases: dict[str, str] | None = None, zetaris: bool = False,
             kubernetes: dict[str, Any] | None = None, loki: dict[str, Any] | None = None) -> dict[str, Any]:
    """docker: {"project": ..., "service_label": ..., "socket": ...} or None to skip.

    zetaris: this deployment's data layer is Zetaris, so databases are registered there and the generated
    checks and evidence are Spark SQL over <name>.<table>, not direct connections.
    kubernetes: {"url", "token", "namespace", "service_label"} (all optional in a cluster) or None to skip.
    loki: {"url", "tenant", "token"} or None to skip."""
    plan: dict[str, Any] = {
        "system": {"name": "my-system", "description": ""}, "docker": {}, "services": {}, "runtime_facts": "",
        "sources": {}, "catalog": [], "topology": {}, "checks": [], "evidence": [], "notes": [], "errors": [],
        "externals": [], "log_source": None, "databases": {},
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


    if kubernetes is not None:
        try:
            k = discover_kubernetes(kubernetes.get("url", ""), kubernetes.get("token", ""),
                                    kubernetes.get("namespace", ""), kubernetes.get("service_label", ""))
            plan["sources"]["k8s"] = k["source"]
            plan["kubernetes"] = {"namespace": k["namespace"]}
            for svc, facts in k["services"].items():
                plan["services"].setdefault(svc, facts)
            for svc, deps in k["topology"].items():
                plan["topology"].setdefault(svc, [])
                plan["topology"][svc] += [d for d in deps if d not in plan["topology"][svc]]
            plan["checks"] += k["checks"]
            plan["runtime_facts"] = (plan["runtime_facts"] + "\n" if plan["runtime_facts"] else "") + k["runtime_facts"]
            plan["notes"] += k["notes"]
            env_hits.update(k["env_hits"])
            for svc, hosts in k["log_hits"].items():
                log_hits.setdefault(svc, {}).update(hosts)
            internal |= set(k["internal"])
            plan["log_source"] = plan["log_source"] or {"type": "kubernetes", "source": "k8s"}
        except Exception as e:
            plan["errors"].append(f"Kubernetes: {e}")

    if loki is not None and loki.get("url"):
        try:
            lk = discover_loki(loki["url"], tenant=loki.get("tenant", ""), token=loki.get("token", ""))
            plan["sources"]["loki"] = lk["source"]
            plan["catalog"].append(lk["catalog"])
            plan["checks"] += lk["checks"]
            plan["evidence"] += lk["evidence"]
            for svc in lk["services"]:
                plan["services"].setdefault(svc, {"what": "TODO: describe what this service does"})
            plan["log_source"] = plan["log_source"] or {"type": "loki", "source": "loki", "label": lk["label"]}
        except Exception as e:
            plan["errors"].append(f"Loki at {loki['url']}: {e}")

    if prometheus_url:
        try:
            p = discover_prometheus(prometheus_url)
            plan["sources"]["prom"] = {"type": "prometheus", "url": prometheus_url}
            plan["catalog"].append(p["catalog"])
            # a monitoring name that is a known service under another name: attach its checks to that service, so the
            # map and the blast radius see one service (the PromQL keeps the monitoring name)
            known = set(plan["services"])
            names = set(p["services"]) | {c["service"] for c in p["checks"]}
            alias = {n: m for n in names if n not in known and (m := match_service(n, known))}
            for c in p["checks"]:
                c["service"] = alias.get(c["service"], c["service"])
            plan["checks"] += p["checks"]
            plan["notes"] += p["notes"] + [f"Prometheus calls '{n}' what Docker calls '{m}': its metrics are attached to '{m}'."
                                           for n, m in sorted(alias.items())]
            for svc in p["services"]:
                plan["services"].setdefault(alias.get(svc, svc), {"what": "TODO: describe what this service does"})
        except Exception as e:
            plan["errors"].append(f"Prometheus at {prometheus_url}: {e}")

    for name, url in (databases or {}).items():
        try:
            s = discover_sql(name, url, target="zetaris" if zetaris else "direct")
            if zetaris:
                plan["databases"][name] = sqlalchemy_url(url)  # registered with Zetaris; the secret goes to .env
                if s["dialect"] == "postgresql":  # live internals (sessions, locks, indexes) are not tables Zetaris can register
                    plan["sources"][f"{name}_live"] = {"type": "postgres", "url": sqlalchemy_url(url)}
                    direct = discover_sql(name, url, target="direct")  # the live-session evidence only exists there
                    s["evidence"] = s["evidence"] + [{**e, "source": f"{name}_live"} for e in direct["evidence"]
                                                     if "pg_stat_activity" in e.get("query", "")]
                    plan["checks"].append(_connections_check(
                        name, f"{name}_live", _database_service(url, plan["services"]) or name))
                    s["catalog"] += (f"\n  Live Postgres internals are NOT in Zetaris. Query source {name}_live (PostgreSQL SQL) for: "
                                     "pg_stat_activity (who is connected, what they run and wait on), pg_locks, pg_indexes, "
                                     "pg_stat_user_tables (seq_scan vs idx_scan), pg_stat_statements (if enabled).")
            else:
                plan["sources"][name] = {"type": "postgres" if s["dialect"] == "postgresql" else "sql",
                                         "url": sqlalchemy_url(url)}
                if s["dialect"] == "postgresql":
                    plan["checks"].append(_connections_check(name, name, _database_service(url, plan["services"]) or name))
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
                plan["log_source"] = {"type": "sql", "source": "zetaris" if zetaris else name,
                                      "dialect": "spark" if zetaris else s["dialect"], **s["logs"]}
        except Exception as e:
            plan["errors"].append(f"Database {name}: {str(e).splitlines()[0]}")
    if zetaris:
        _finish_zetaris(plan, prometheus_url, loki)
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
    plan["service_facts"] = {n: knowledge.facts_for(n, plan["services"], plan["topology"], plan["checks"])
                             for n in plan["services"]}
    services = sorted(plan["services"])
    plan["system"]["description"] = (
        f"{len(services)} services: {', '.join(services)}. " if services else ""
    ) + "TODO: one or two sentences on what the system does and how requests flow."
    return plan


def _finish_zetaris(plan: dict[str, Any], prometheus_url: str, loki: dict[str, Any] | None) -> None:
    """Zetaris is the data layer: it is always a source, it reads Prometheus and Loki itself (promql / logql), and the
    sources it covers become internal, so the Investigator asks Zetaris and the Watcher keeps its fast direct path."""
    rest: dict[str, Any] = {}
    if prometheus_url and "prom" in plan["sources"]:
        rest["prometheus"] = prometheus_url
        plan["sources"]["prom"]["internal"] = True
        plan["catalog"] = [c.replace("prom (PromQL, instant queries;", "zetaris, command `promql <expression>` (PromQL instant queries;")
                           for c in plan["catalog"]]
    if loki and loki.get("url") and "loki" in plan["sources"]:
        rest["loki"] = loki["url"]
        if loki.get("tenant"):
            rest["loki_headers"] = {"X-Scope-OrgID": loki["tenant"]}
        plan["sources"]["loki"]["internal"] = True
        plan["catalog"] = [c.replace("loki (Loki, LogQL)", "zetaris, command `logql <expression>` (Loki, LogQL)") for c in plan["catalog"]]
    plan["sources"]["zetaris"] = {**source_config(), **({"rest": rest} if rest else {})}
    if plan.get("docker"):
        plan["docker"]["internal"] = True  # container state, events and logs are mirrored into Zetaris tables
    plan["zetaris"] = True
    plan["state_url"] = "${OVERWATCH_STATE_URL}"
    if not os.environ.get("OVERWATCH_STATE_URL"):
        plan["errors"].append("Zetaris is the data layer, so Overwatch needs a Postgres of its own for its history and "
                              "telemetry: set OVERWATCH_STATE_URL (the bundled docker-compose in deploy/ provides one).")


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
            "external": "yes",
        }
        if url := str(ext.get("health_url", "")).strip():
            checks.append({"name": f"{_key(name)}_up", "kind": "probe", "service": name, "url": url,
                           "timeout_ms": 3000, "severity": "high", "description": f"{name} answered its health/status URL"})
        text = str(ext.get("log_text", "")).strip().replace("'", "")
        if text and log_source.get("type") == "sql":
            dialect = log_source.get("dialect")
            window = ("current_timestamp() - INTERVAL 2 MINUTES" if dialect == "spark" else
                      "now() - interval '2 minutes'" if dialect == "postgresql" else "datetime('now', '-2 minutes')")
            checks.append({
                "name": f"{_key(name)}_errors", "service": name, "source": log_source["source"],
                "query": _lit(f"SELECT count(*) AS value FROM {log_source['table']}\n"
                              f"WHERE upper({log_source['level']}) IN ('ERROR', 'FATAL', 'CRITICAL')\n"
                              f"  AND message LIKE '%{text}%' AND {log_source['ts']} > {window}"),
                "threshold": 5, "severity": "high", "description": f"errors mentioning {name}, last 2 minutes",
            })
        elif text and log_source.get("type") == "loki":
            callers = ext.get("callers") or []
            selector = f'{log_source["label"]}=~"{"|".join(callers)}"' if callers else f'{log_source["label"]}=~".+"'
            checks.append({
                "name": f"{_key(name)}_errors", "service": name, "source": log_source["source"],
                "query": f'sum(count_over_time({{{selector}}} |= "{text}" [2m]))', "threshold": 5, "severity": "high",
                "description": f"log lines mentioning {name}, last 2 minutes",
            })
        elif text and log_source.get("type") in ("docker", "kubernetes"):
            checks.append({
                "name": f"{_key(name)}_errors", "service": name, "source": log_source["source"],
                "query": f"count {','.join(ext.get('callers') or ['*'])} 2 {text}", "threshold": 5, "severity": "high",
                "description": f"error log lines mentioning {name}, last 2 minutes",
            })
    return topology, services, checks


_NOTIFIER_SECRETS = {("slack", "webhook_url"): "SLACK_WEBHOOK_URL", ("jira", "api_token"): "JIRA_API_TOKEN",
                     ("webhook", "url"): "OW_WEBHOOK_URL"}


def _notifiers(chosen: dict[str, dict[str, Any]], env: dict[str, str]) -> list[dict[str, Any]]:
    """Slack / Jira / webhook from the setup page; secret fields go to .env."""
    out = []
    for kind in ("slack", "jira", "webhook"):
        cfg = chosen.get(kind) or {}
        if not cfg.get("enabled"):
            continue
        entry: dict[str, Any] = {"type": kind}
        for field, value in cfg.items():
            if field == "enabled" or field.endswith("_set") or not value:
                continue
            if var := _NOTIFIER_SECRETS.get((kind, field)):
                env[var] = str(value)
                value = f"${{{var}}}"
            entry[field] = value
        out.append(entry)
    return out


def render(plan: dict[str, Any], llm: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """YAML text for overwatch.yaml, and the secrets (env var -> value) that belong in .env."""
    env: dict[str, str] = {}
    sources = {}
    for name, src in plan["sources"].items():
        if src.get("type") in ("postgres", "sql", "zetaris") and "url" in src:
            var = _env_name(name)
            env[var] = src["url"]
            sources[name] = {**src, "url": f"${{{var}}}"}
        elif src.get("token") or src.get("bearer_token"):
            field = "token" if src.get("token") else "bearer_token"
            var = "OW_" + re.sub(r"[^A-Z0-9]", "_", name.upper()) + "_TOKEN"
            env[var] = src[field]
            sources[name] = {**src, field: f"${{{var}}}"}
        else:
            sources[name] = src

    for name, url in (plan.get("databases") or {}).items():  # kept so the data layer can re-register them
        env[_env_name(name)] = url
    llm = {k: v.strip() if isinstance(v, str) else v for k, v in (llm or {}).items()}  # a typed space must not reach .env
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
    if plan.get("state_url"):
        doc["state_url"] = plan["state_url"]
    if plan.get("databases"):  # the data layer re-registers these at every start, so a restarted Zetaris heals itself
        doc["register"] = {name: f"${{{_env_name(name)}}}" for name in plan["databases"]}
    if sources:
        doc["sources"] = sources
    if plan.get("catalog"):
        doc["catalog"] = _lit(plan["catalog"])
    if topology:
        doc["topology"] = topology
    if evidence:
        doc["evidence"] = evidence
    doc["checks"] = checks
    settings = plan.get("settings") or {}
    doc["watch"] = {"interval_seconds": 10, "confirm_samples": 2,
                    **{k: int(v) for k, v in (settings.get("watch") or {}).items() if v not in (None, "")}}
    doc["llm"] = {
        "base_url": "${LLM_BASE_URL:-http://127.0.0.1:8088/v1}",
        "api_key": "${LLM_API_KEY}",
        "investigator_model": "${OVERWATCH_MODEL:-meterless/text.deep}",
        "triage_model": "${OVERWATCH_TRIAGE_MODEL:-meterless/text.fast}",
        "max_steps": int((settings.get("ai") or {}).get("max_steps") or 8),
    }
    doc["memory"] = {"type": "hmem", "url": "${MEMORY_URL:-http://127.0.0.1:8099}"}
    doc["approval"] = settings.get("approval") if settings.get("approval") in ("manual", "auto") else "manual"
    doc["notifiers"] = [{"type": "console"}, *_notifiers(settings.get("notifiers") or {}, env)]

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
