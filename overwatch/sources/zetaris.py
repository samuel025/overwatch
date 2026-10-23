"""Zetaris, Overwatch's data layer: one federated SQL endpoint over every system the user connected.

Reached over JDBC with Zetaris's own driver (the docs say not to substitute a Hive/Spark driver), through
`jaydebeapi`, which needs Java. Tables are `<datasource>.<table>`; the SQL is Spark SQL ("Lightning SQL").
Config (normally from the deployment's .env, never from the end user):
  jdbc_url      jdbc:... endpoint of the Zetaris workspace
  driver_class  the driver's Java class (from the Zetaris driver you downloaded)
  driver_jar    path to the driver JAR
  username, password
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .base import DataSource, SourceError
from .sql import check_read_only

_META = re.compile(r"^(show|describe|desc)\b", re.IGNORECASE)

LANGUAGE = (
    "Zetaris Lightning SQL (Spark SQL). Tables are <datasource>.<table>, e.g. shopdb.orders; a query may join "
    "tables from different datasources. Time windows: current_timestamp() - INTERVAL 5 MINUTES. Use backticks "
    "around odd identifiers. Discover with SHOW DATASOURCES."
)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


class ZetarisSource(DataSource):
    language = LANGUAGE

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        for key in ("jdbc_url", "driver_class"):
            if not cfg.get(key):
                raise SourceError(f"source '{name}' needs '{key}' (see the Zetaris client setup)")
        self._conn: Any = None

    def _connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            import jaydebeapi  # optional dependency; also needs a JRE and the Zetaris driver JAR
        except ImportError as e:
            raise SourceError("Zetaris access needs 'jaydebeapi' (pip install overwatch[zetaris]) and Java") from e
        try:
            self._conn = jaydebeapi.connect(
                self.cfg["driver_class"], self.cfg["jdbc_url"],
                [self.cfg.get("username", ""), self.cfg.get("password", "")], self.cfg.get("driver_jar") or None)
        except Exception as e:
            raise SourceError(f"could not connect to Zetaris: {str(e).splitlines()[0]}") from e
        return self._conn

    def _run(self, sql: str, limit: int | None) -> list[dict[str, Any]]:
        conn = self._connect()
        cur = conn.cursor()
        try:
            cur.execute(sql)
            if cur.description is None:
                return []
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
        except Exception as e:
            self._conn = None  # a broken connection should not poison the next query
            raise SourceError(str(e).splitlines()[0]) from e
        finally:
            try:
                cur.close()
            except Exception:
                pass
        return [{c: _jsonable(v) for c, v in zip(cols, r)} for r in rows]

    def query(self, q: str) -> list[dict[str, Any]]:
        stripped = q.strip().rstrip(";").strip()
        sql = stripped if _META.match(stripped) and ";" not in stripped else check_read_only(q)
        return self._run(sql, self.max_rows)

    def admin(self, statements: list[str]) -> list[str]:
        """Run registration statements (CREATE DATASOURCE ...), one at a time. Used by Overwatch's own setup
        flow with the deployment's admin account; never reachable by agents (they only call query())."""
        done = []
        for s in statements:
            self._run(s, None)
            done.append(s.split("(")[0].strip()[:80])
        return done

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
