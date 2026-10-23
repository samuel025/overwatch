"""A small read-only client for the Docker Engine API over its unix socket.

Used for discovery, the built-in `docker` data source, `kind: container` checks and the event
listener. Only GET requests: Overwatch never changes your containers.
"""

from __future__ import annotations

import json
import struct
import time
from typing import Any

import httpx

COMPOSE_SERVICE = "com.docker.compose.service"
COMPOSE_PROJECT = "com.docker.compose.project"
COMPOSE_DEPENDS_ON = "com.docker.compose.depends_on"


class DockerError(Exception):
    pass


class DockerAPI:
    def __init__(self, socket: str = "/var/run/docker.sock", timeout: float = 10):
        self.socket = socket
        self.client = httpx.Client(
            transport=httpx.HTTPTransport(uds=socket), base_url="http://docker", timeout=timeout
        )

    def _get(self, path: str, **params) -> Any:
        try:
            r = self.client.get(path, params=params)
        except httpx.HTTPError as e:
            raise DockerError(f"cannot reach Docker at {self.socket}: {e}") from e
        if r.status_code >= 400:
            raise DockerError(f"Docker API {path} returned {r.status_code}: {r.text[:200]}")
        return r

    def available(self) -> bool:
        try:
            return self._get("/_ping").text.strip() == "OK"
        except DockerError:
            return False

    def containers(self, project: str | None = None, include_stopped: bool = True) -> list[dict[str, Any]]:
        filters = {"label": [f"{COMPOSE_PROJECT}={project}"]} if project else {}
        return self._get("/containers/json", all=str(include_stopped).lower(), filters=json.dumps(filters)).json()

    def inspect(self, container_id: str) -> dict[str, Any]:
        return self._get(f"/containers/{container_id}/json").json()

    def projects(self) -> list[str]:
        return sorted({c["Labels"].get(COMPOSE_PROJECT) for c in self.containers() if c["Labels"].get(COMPOSE_PROJECT)})

    def logs(self, container_id: str, tail: int = 50) -> list[tuple[str, str]]:
        """Last `tail` lines as (timestamp, line), demultiplexing Docker's stdout/stderr frames."""
        raw = self._get(f"/containers/{container_id}/logs", stdout=1, stderr=1, tail=tail, timestamps=1).content
        lines, i = [], 0
        if raw[:1] in (b"\x00", b"\x01", b"\x02") and len(raw) >= 8:  # multiplexed (no TTY)
            chunks = []
            while i + 8 <= len(raw):
                size = struct.unpack(">I", raw[i + 4 : i + 8])[0]
                chunks.append(raw[i + 8 : i + 8 + size])
                i += 8 + size
            raw = b"".join(chunks)
        for line in raw.decode("utf-8", "replace").splitlines():
            ts, _, text = line.partition(" ")
            lines.append((ts, text))
        return lines

    def events(self, since_seconds: int = 900, filters: dict[str, list[str]] | None = None) -> list[dict[str, Any]]:
        """Past events (non-streaming): container die/start/oom/kill/restart in the window."""
        now = int(time.time())
        f = {"type": ["container"], **(filters or {})}
        r = self._get("/events", since=now - since_seconds, until=now, filters=json.dumps(f))
        return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def depends_on(labels: dict[str, str]) -> list[str]:
    """Compose writes depends_on as 'svc:condition:restart,svc2:...'."""
    raw = labels.get(COMPOSE_DEPENDS_ON, "")
    return [part.split(":")[0] for part in raw.split(",") if part.strip()]
