"""Draft the service descriptions a person would otherwise have to write.

Setup leaves "TODO: describe what this service does" because the Investigator reads those sentences as fact and only a
person knows what a service is for. Overwatch can still write a good first version from what it already knows
(the image, how it runs, who calls whom, which checks watch it), so the person edits instead of starting from nothing.

Rules this keeps:
- only facts Overwatch holds go to the model (no environment values, no tokens, no connection strings);
- only empty or TODO fields are drafted, never text a person wrote;
- the model is told not to guess a business purpose, so a draft states what is known, and what is not;
- drafts are returned as suggestions: nothing is saved until the person saves.
"""

from __future__ import annotations

import re
import tempfile
from typing import Any

from . import knowledge
from .config import LLMConfig
from .llm import LLM, LLMError

SYSTEM_PROMPT = """You write short descriptions of the services in a software system for an on-call engineer, from facts you are given.

Rules:
- Use only the facts given. Never invent what a service is for. If the facts do not say what an application does, say what
  it connects to and what connects to it, and that its purpose is not stated.
- Say what a service is only when its facts say "identified as: ...", and then use that wording. For a service marked
  "software not identified", do not say what it is or what it does: describe its place (what it calls, what calls it) and
  say that what it is has not been stated.
- One sentence per service, at most 25 words, plain language, no marketing words.
- The system description is one or two sentences: what the services are and how requests flow, from the topology only.
- Never mention anything that is not in the facts.

Reply with exactly one JSON object: {"system": "<text or empty>", "services": {"<service name>": "<one sentence>"}}
Include only the services you are asked to describe."""

MAX_LEN = 260


# Software an image name states outright. Only these may be described as what they are; anything else is described by
# its place in the system, so a model cannot turn "mq" inside a made-up name into "a message queue".
_KNOWN = [
    (r"postgres", "PostgreSQL database"), (r"mysql", "MySQL database"), (r"mariadb", "MariaDB database"),
    (r"mongo", "MongoDB database"), (r"redis", "Redis cache / key-value store"), (r"memcached", "Memcached cache"),
    (r"nginx", "nginx web server / reverse proxy"), (r"httpd|apache", "Apache web server"), (r"traefik", "Traefik proxy"),
    (r"haproxy", "HAProxy load balancer"), (r"envoy", "Envoy proxy"), (r"prometheus", "Prometheus metrics server"),
    (r"grafana/loki|(^|/)loki", "Grafana Loki log store"), (r"grafana", "Grafana dashboards"),
    (r"rabbitmq", "RabbitMQ message broker"), (r"kafka", "Apache Kafka"), (r"zookeeper", "ZooKeeper"),
    (r"elasticsearch", "Elasticsearch"), (r"minio", "MinIO object storage"), (r"jaeger", "Jaeger tracing"),
]


def identify(image: str) -> str | None:
    """Well-known software named by an image (the repository name, not the tag); None for anything else."""
    repo = str(image or "").lower().split(":")[0]
    for pattern, label in _KNOWN:
        if re.search(pattern, repo):
            return label
    return None


def _line(svc: dict[str, Any], topology: dict[str, list[str]], checks: list[dict[str, Any]]) -> str:
    name = svc["name"]
    calls = topology.get(name, [])
    called_by = sorted(k for k, v in topology.items() if name in v)
    mine = [c.get("name", "") for c in checks if c.get("service") == name][:8]
    parts = []
    if svc.get("image"):
        parts.append(f"image {svc['image']}")
    kind = identify(svc.get("image", ""))
    parts.append(f"identified as: {kind}" if kind else "software not identified (do not say what it is)")
    if svc.get("runs_on"):
        parts.append(f"runs as: {svc['runs_on']}")
    parts.append("calls: " + (", ".join(calls) if calls else "nothing"))
    parts.append("called by: " + (", ".join(called_by) if called_by else "nothing"))
    if mine:
        parts.append("watched by checks: " + ", ".join(mine))
    return f"- {name}: " + "; ".join(parts)


def missing(system: dict[str, Any], services: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """What still needs a description: the system (empty or TODO) and each service (empty or TODO)."""
    sys_todo = knowledge.is_placeholder(system.get("description")) or not str(system.get("description", "")).strip()
    names = [s["name"] for s in services if knowledge.is_placeholder(s.get("what")) or not str(s.get("what", "")).strip()]
    return sys_todo, names


def draft(llm_cfg: LLMConfig, system: dict[str, Any], services: list[dict[str, Any]], topology: dict[str, list[str]],
          checks: list[dict[str, Any]], llm: LLM | None = None) -> dict[str, Any]:
    """Returns {"system": str | None, "services": {name: sentence}}: suggestions for the empty fields only."""
    want_system, names = missing(system, services)
    if not want_system and not names:
        return {"system": None, "services": {}}
    known = {s["name"]: s for s in services}
    facts = "\n".join(_line(known[n], topology, checks) for n in [s["name"] for s in services])
    ask = []
    if names:
        ask.append("Describe these services: " + ", ".join(names) + ".")
    ask.append("Also write the system description." if want_system else "Do not write a system description (leave it empty).")
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"System name: {system.get('name') or 'unnamed'}\n\nFacts about every service:\n{facts}\n\n" + " ".join(ask)}]
    client = llm or LLM(llm_cfg, trace_dir=tempfile.mkdtemp(prefix="overwatch-draft-"))
    last: Exception | None = None
    for model, patience in ((llm_cfg.triage_model, 25), (llm_cfg.summary_model or llm_cfg.investigator_model, 90)):
        if not model:
            continue
        try:
            reply = client.chat_json(messages, model, agent="draft", patience=patience)
            break
        except LLMError as e:  # the fast model may be slow today: the main one gets a longer turn
            last = e
    else:
        raise LLMError(f"could not draft: {last}" if last else "no AI model is configured")
    out: dict[str, str] = {}
    for name, text in (reply.get("services") or {}).items():
        text = " ".join(str(text).split())[:MAX_LEN].strip()
        if name in names and text and not knowledge.is_placeholder(text):
            out[name] = text
    sys_text = " ".join(str(reply.get("system") or "").split())[:MAX_LEN * 2].strip() if want_system else ""
    return {"system": sys_text or None, "services": out}
