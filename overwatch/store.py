"""Incident persistence in SQLite, so a restart keeps history and open incidents."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .models import Incident


class IncidentStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, opened_at TEXT NOT NULL, "
            "status TEXT NOT NULL, data TEXT NOT NULL)"
        )
        self._db.commit()

    def save(self, incident: Incident) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO incidents (id, opened_at, status, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data",
                (incident.id, incident.opened_at, incident.status, json.dumps(incident.to_dict(), default=str)),
            )
            self._db.commit()

    def load(self, limit: int = 200) -> list[Incident]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM incidents ORDER BY opened_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [Incident.from_dict(json.loads(r[0])) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
