"""In-process event bus: agents publish what they are doing, the UI streams it over SSE.

Agents run in worker threads; subscribers are asyncio queues, so publishing hops onto each
subscriber's event loop. A ring buffer lets a newly opened UI replay recent activity.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
import json
import math
from collections import deque
from typing import Any


class EventBus:
    def __init__(self, keep: int = 600):
        self._buffer: deque[dict[str, Any]] = deque(maxlen=keep)
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

    def emit(self, type_: str, **data: Any) -> None:
        event = {"id": next(self._ids), "ts": time.time(), "type": type_, **data}
        with self._lock:
            self._buffer.append(event)
            subscribers = list(self._subscribers)
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:  # loop closed: the client went away
                self.unsubscribe(queue)

    def subscribe(self) -> tuple[asyncio.Queue, list[dict[str, Any]]]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append((asyncio.get_running_loop(), queue))
            return queue, list(self._buffer)

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [(lp, q) for lp, q in self._subscribers if q is not queue]


def finite(value: Any) -> Any:
    """Replace NaN/Infinity with None, recursively: they are not valid JSON and a browser drops the whole message."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def to_json(event: dict[str, Any]) -> str:
    """Serialize an event for the browser (server-sent events)."""
    try:
        return json.dumps(event, default=str, allow_nan=False)
    except ValueError:
        return json.dumps(finite(event), default=str, allow_nan=False)
