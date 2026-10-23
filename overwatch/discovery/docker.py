"""Discover services, dependencies, liveness and health endpoints from Docker / Docker Compose."""

from __future__ import annotations

from typing import Any

import httpx

from ..docker_api import COMPOSE_SERVICE, DockerAPI, DockerError, depends_on
from .externals import hosts_in_env, hosts_in_text
from ..sources.docker import _ERRORISH

# Recognised images: what they are, so the draft service knowledge is not empty.
KNOWN_IMAGES = {
    "postgres": "PostgreSQL database server", "mysql": "MySQL database server", "mariadb": "MariaDB database server",
    "mongo": "MongoDB database", "redis": "Redis cache / key-value store", "rabbitmq": "RabbitMQ message broker",
    "kafka": "Kafka event streaming broker", "nginx": "nginx web server / reverse proxy", "traefik": "Traefik proxy",
    "elasticsearch": "Elasticsearch search engine", "prometheus": "Prometheus metrics server",
    "grafana": "Grafana dashboards", "memcached": "Memcached cache",
}
DATABASE_IMAGES = ("postgres", "mysql", "mariadb", "mongo", "redis")
HEALTH_PATHS = ("/health", "/healthz", "/ready", "/status")


def _what(image: str) -> str:
    base = image.split("/")[-1].split(":")[0].lower()
    return next((v for k, v in KNOWN_IMAGES.items() if k in base), "")


def _probe_url(ports: list[dict[str, Any]], host: str) -> str | None:
    """The first published port that answers 2xx on a common health path."""
    for p in ports:
        if p.get("Type") != "tcp" or not p.get("PublicPort"):
            continue
        for path in HEALTH_PATHS:
            url = f"http://{host}:{p['PublicPort']}{path}"
            try:
                if httpx.get(url, timeout=1.5).status_code < 300:
                    return url
            except httpx.HTTPError:
                break  # nothing HTTP on this port
    return None


def discover_docker(project: str | None, service_label: str = COMPOSE_SERVICE, socket: str = "/var/run/docker.sock",
                    probe_host: str = "127.0.0.1") -> dict[str, Any]:
    api = DockerAPI(socket)
    if not api.available():
        raise DockerError(f"Docker is not reachable at {socket}")
    projects = api.projects()
    if not project:
        if len(projects) == 1:
            project = projects[0]
        elif projects:
            return {"needs_project": True, "projects": projects}
    containers = api.containers(project)
    if not containers:
        raise DockerError(f"no containers found for project '{project}'")

    # compose service name -> our service name (they differ when a custom service_label groups replicas)
    compose_to_service = {c["Labels"].get(COMPOSE_SERVICE): c["Labels"].get(service_label) for c in containers}
    services: dict[str, dict[str, Any]] = {}
    topology: dict[str, set[str]] = {}
    checks: list[dict[str, Any]] = []
    notes: list[str] = []
    env_hits: dict[str, dict[str, str]] = {}
    log_hits: dict[str, dict[str, int]] = {}
    internal: set[str] = set()
    for c in containers:
        name = c["Labels"].get(service_label)
        if not name:
            continue
        info = api.inspect(c["Id"])
        host = info["HostConfig"]
        internal |= {name, c["Labels"].get(COMPOSE_SERVICE, ""), info["Config"].get("Hostname", "")}
        internal |= {n.lstrip("/") for n in c.get("Names", [])}
        env_hits.setdefault(name, {}).update(hosts_in_env(info["Config"].get("Env") or []))
        if c.get("State") == "running":
            try:
                errors = [line for _, line in api.logs(c["Id"], tail=300) if _ERRORISH.search(line)]
                for h, n in hosts_in_text(errors).items():
                    log_hits.setdefault(name, {})[h] = log_hits.get(name, {}).get(h, 0) + n
            except DockerError:
                pass
        svc = services.setdefault(name, {
            "image": c.get("Image", ""), "instances": 0, "databases": False,
            "cpu_limit": (host.get("NanoCpus") or 0) / 1e9 or None,
            "memory_limit_mb": (host.get("Memory") or 0) // 2**20 or None,
            "restart_policy": (host.get("RestartPolicy") or {}).get("Name") or "no",
            "ports": [], "probe": None, "running": False,
        })
        svc["instances"] += 1
        svc["running"] = svc["running"] or c.get("State") == "running"
        svc["databases"] = svc["databases"] or any(d in svc["image"].lower() for d in DATABASE_IMAGES)
        svc["ports"] += [p for p in c.get("Ports", []) if p.get("PublicPort")]
        deps = {compose_to_service.get(d, d) for d in depends_on(c["Labels"])} - {name, None}
        topology.setdefault(name, set()).update(deps)

    for name, svc in services.items():
        if not svc["databases"] and svc["running"]:
            svc["probe"] = _probe_url(svc["ports"], probe_host)
        checks.append({
            "name": f"{name.replace('-', '_')}_running", "kind": "container", "service": name,
            "description": f"{name} has a running container", "severity": "critical",
            "why": "liveness from Docker: fires when no container of the service is running",
        })
        if svc["probe"]:
            checks.append({
                "name": f"{name.replace('-', '_')}_probe", "kind": "probe", "service": name, "url": svc["probe"],
                "timeout_ms": 1500, "description": f"{name} answered its health check", "severity": "critical",
                "why": f"found a health endpoint at {svc['probe']}",
            })

    knowledge = {}
    for name, svc in services.items():
        runs = [f"{svc['instances']} Docker container{'s' if svc['instances'] > 1 else ''}"]
        if svc["cpu_limit"]:
            runs.append(f"{svc['cpu_limit']:g} CPU")
        if svc["memory_limit_mb"]:
            runs.append(f"{svc['memory_limit_mb']} MB memory limit")
        runs.append(f"restart policy {svc['restart_policy']}")
        knowledge[name] = {
            "what": _what(svc["image"]) or "TODO: describe what this service does",
            "runs_on": ", ".join(runs),
            "image": svc["image"],
        }
    if any(s["instances"] > 1 for s in services.values()):
        notes.append("Some services run several containers; Overwatch treats them as instances of one service.")

    runtime_facts = (
        f"Runs on Docker Compose (project {project}) on one host. There is no Kubernetes unless stated.\n"
        "Container exit codes: 0 = clean stop (stopped on purpose), 137 = killed (SIGKILL, usually out of memory "
        "when oom_killed is true), 143 = terminated (SIGTERM), other = crash.\n"
        "Restart policies: 'unless-stopped'/'always' restart after a crash but not after a deliberate stop; "
        "'no' never restarts."
    )
    return {
        "project": project,
        "projects": projects,
        "service_label": service_label,
        "services": knowledge,
        "topology": {k: sorted(v) for k, v in topology.items()},
        "checks": checks,
        "runtime_facts": runtime_facts,
        "notes": notes,
        "databases": [n for n, s in services.items() if s["databases"]],
        "env_hits": env_hits,
        "log_hits": log_hits,
        "internal": sorted(h.lower() for h in internal if h),
    }
