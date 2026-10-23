"""Pushed event streams: react the instant something happens instead of on the next check pass.

DockerEvents follows the Docker Engine event stream (the same API `docker events` uses) and reports
when a container dies, is OOM-killed, or starts again. On Kubernetes the equivalent is a watch on
pod status; the callback contract is the same.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

import httpx

OnContainerEvent = Callable[[str, str, str, str], None]  # (kind "down"/"up", service, instance, detail)


class DockerEvents:
    def __init__(self, cfg: dict[str, Any], on_event: OnContainerEvent, on_error: Callable[[str], None] = print):
        self.socket = cfg.get("socket", "/var/run/docker.sock")
        self.service_label = cfg.get("service_label", "com.docker.compose.service")
        self.instance_label = cfg.get("instance_label", self.service_label)
        self.filters = {"type": ["container"], **cfg.get("filters", {})}
        self.on_event = on_event
        self.on_error = on_error
        self._stop = threading.Event()
        self._oom: set[str] = set()  # container ids that were just OOM-killed (the "die" event follows)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name="docker-events").start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                transport = httpx.HTTPTransport(uds=self.socket)
                with httpx.Client(transport=transport, base_url="http://docker", timeout=None) as client:
                    params = {"filters": json.dumps(self.filters)}
                    with client.stream("GET", "/events", params=params) as stream:
                        backoff = 1.0
                        for line in stream.iter_lines():
                            if self._stop.is_set():
                                return
                            if line.strip():
                                self.handle(json.loads(line))
            except Exception as e:  # docker restarted, socket missing...: keep trying
                self.on_error(f"docker events: {e}")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    def handle(self, event: dict[str, Any]) -> None:
        action = event.get("Action") or event.get("status") or ""
        attrs = (event.get("Actor") or {}).get("Attributes") or {}
        service = attrs.get(self.service_label)
        if not service:
            return
        instance = attrs.get(self.instance_label, service)
        cid = (event.get("Actor") or {}).get("ID", "")
        if action == "oom":
            self._oom.add(cid)
        elif action == "die":
            code = attrs.get("exitCode", "?")
            reason = "was killed for using too much memory (OOM)" if cid in self._oom else f"exited (exit code {code})"
            self._oom.discard(cid)
            self.on_event("down", service, instance, f"container {instance} {reason} at {time.strftime('%H:%M:%S', time.gmtime())} UTC")
        elif action == "start":
            self.on_event("up", service, instance, f"container {instance} started")


LISTENER_TYPES = {"docker": DockerEvents}
