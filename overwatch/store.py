"""Overwatch's own data: incidents, the change log, and every check's value over time.

Lives in SQLite by default (a file next to the traces). Point `state_url` at Postgres instead and the same
tables become something other tools can read: with Zetaris as the data layer, Overwatch registers this database
there, so a question like "what changed just before this metric spiked?" is one query across the user's
databases and Overwatch's own history.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text

from .models import Incident


class IncidentStore:
    def __init__(self, target: str | Path):
        raw = str(target)
        if "://" in raw:
            self.url, self.path = raw, None
            self.engine = create_engine(raw, pool_pre_ping=True)
        else:
            self.path = Path(raw)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.url = f"sqlite:///{self.path.resolve()}"
            self.engine = create_engine(self.url, connect_args={"check_same_thread": False})
        self.dialect = self.engine.dialect.name
        self._lock = threading.Lock()
        with self.engine.begin() as db:
            db.execute(text("CREATE TABLE IF NOT EXISTS incidents (id TEXT PRIMARY KEY, opened_at TEXT NOT NULL, "
                            "status TEXT NOT NULL, data TEXT NOT NULL)"))
            # Releases and config changes: reported (overwatch change, POST /api/changes) or learned (change feeds).
            db.execute(text("CREATE TABLE IF NOT EXISTS changes (ts TEXT NOT NULL, service TEXT NOT NULL, version TEXT, "
                            "kind TEXT NOT NULL DEFAULT 'deploy', author TEXT, description TEXT)"))
            # Every check's value on every pass, so history survives restarts and can be queried.
            db.execute(text("CREATE TABLE IF NOT EXISTS check_samples (ts TEXT NOT NULL, check_name TEXT NOT NULL, "
                            "service TEXT, value DOUBLE PRECISION, failing INTEGER NOT NULL DEFAULT 0)"))
            # Docker telemetry (see telemetry.py): recent container state, events and log lines, queryable like the rest.
            db.execute(text("CREATE TABLE IF NOT EXISTS container_state (ts TEXT NOT NULL, service TEXT, container TEXT, "
                            "state TEXT, exit_code INTEGER, oom_killed INTEGER, restarts INTEGER, health TEXT, "
                            "started_at TEXT, finished_at TEXT)"))
            db.execute(text("CREATE TABLE IF NOT EXISTS container_events (ts TEXT NOT NULL, service TEXT, container TEXT, "
                            "action TEXT, exit_code TEXT, detail TEXT)"))
            db.execute(text("CREATE TABLE IF NOT EXISTS container_logs (ts TEXT NOT NULL, service TEXT, container TEXT, "
                            "line TEXT, important INTEGER NOT NULL DEFAULT 0)"))
        if "ref" not in {c["name"] for c in inspect(self.engine).get_columns("changes")}:
            with self.engine.begin() as db:  # older state files: add the de-duplication key
                db.execute(text("ALTER TABLE changes ADD COLUMN ref TEXT"))
        with self.engine.begin() as db:
            db.execute(text("CREATE INDEX IF NOT EXISTS changes_ref ON changes (ref)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS check_samples_ts ON check_samples (ts)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS container_state_ts ON container_state (ts)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS container_events_ts ON container_events (ts)"))
            db.execute(text("CREATE INDEX IF NOT EXISTS container_logs_ts ON container_logs (ts)"))

    def rows(self, sql: str, **params: Any) -> list[tuple]:
        with self._lock, self.engine.connect() as db:
            return [tuple(r) for r in db.execute(text(sql), params).fetchall()]

    def record_change(self, service: str, version: str = "", kind: str = "deploy", author: str = "",
                      description: str = "", ts: str | None = None, ref: str | None = None) -> dict | None:
        """Add a change. With a `ref` (e.g. "github:org/repo:release:42") a change is stored once; None if seen."""
        row = {"ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"), "service": service,
               "version": version, "kind": kind, "author": author, "description": description, "ref": ref}
        with self._lock, self.engine.begin() as db:
            if ref and db.execute(text("SELECT 1 FROM changes WHERE ref = :ref"), {"ref": ref}).first():
                return None
            db.execute(text("INSERT INTO changes (ts, service, version, kind, author, description, ref) "
                            "VALUES (:ts, :service, :version, :kind, :author, :description, :ref)"), row)
        return row

    def record_samples(self, values: dict[str, float | None], services: dict[str, str], failing: set[str],
                       ts: str | None = None) -> None:
        when = ts or datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows = [{"ts": when, "name": n, "service": services.get(n, ""), "value": v, "failing": int(n in failing)}
                for n, v in values.items()]
        if rows:
            with self._lock, self.engine.begin() as db:
                db.execute(text("INSERT INTO check_samples (ts, check_name, service, value, failing) "
                                "VALUES (:ts, :name, :service, :value, :failing)"), rows)

    def _insert(self, table: str, columns: list[str], rows: list[dict[str, Any]]) -> None:
        if rows:
            with self._lock, self.engine.begin() as db:
                db.execute(text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(':' + c for c in columns)})"),
                           [{c: r.get(c) for c in columns} for r in rows])

    def add_container_state(self, rows: list[dict[str, Any]]) -> None:
        self._insert("container_state", ["ts", "service", "container", "state", "exit_code", "oom_killed", "restarts",
                                         "health", "started_at", "finished_at"], rows)

    def add_container_events(self, rows: list[dict[str, Any]]) -> None:
        self._insert("container_events", ["ts", "service", "container", "action", "exit_code", "detail"], rows)

    def add_container_logs(self, rows: list[dict[str, Any]]) -> None:
        self._insert("container_logs", ["ts", "service", "container", "line", "important"], rows)

    def prune_telemetry(self) -> None:
        """Short memory on purpose: lines that matter (errors, shutdowns, restarts) 6 hours, the rest 1 hour."""
        def ago(**kw: int) -> str:
            return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat(timespec="microseconds")

        with self._lock, self.engine.begin() as db:
            db.execute(text("DELETE FROM container_logs WHERE important = 0 AND ts < :c"), {"c": ago(hours=1)})
            db.execute(text("DELETE FROM container_logs WHERE ts < :c"), {"c": ago(hours=6)})
            db.execute(text("DELETE FROM container_state WHERE ts < :c"), {"c": ago(hours=6)})
            db.execute(text("DELETE FROM container_events WHERE ts < :c"), {"c": ago(hours=24)})

    def prune_samples(self, keep_days: int = 7) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat(timespec="seconds")
        with self._lock, self.engine.begin() as db:
            db.execute(text("DELETE FROM check_samples WHERE ts < :cutoff"), {"cutoff": cutoff})

    def save(self, incident: Incident) -> None:
        with self._lock, self.engine.begin() as db:
            db.execute(text("INSERT INTO incidents (id, opened_at, status, data) VALUES (:id, :at, :status, :data) "
                            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, data = excluded.data"),
                       {"id": incident.id, "at": incident.opened_at, "status": incident.status,
                        "data": json.dumps(incident.to_dict(), default=str)})

    def load(self, limit: int = 200) -> list[Incident]:
        rows = self.rows("SELECT data FROM incidents ORDER BY opened_at DESC LIMIT :n", n=limit)
        return [Incident.from_dict(json.loads(r[0])) for r in rows]

    def close(self) -> None:
        self.engine.dispose()
