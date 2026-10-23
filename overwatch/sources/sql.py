"""Any SQLAlchemy-reachable database: Zetaris, Postgres, MySQL, Snowflake, ...

Queries are restricted to a single SELECT/WITH statement, run with a timeout and a row cap,
and inside a read-only transaction where the database supports it.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import create_engine, text

from .base import DataSource, SourceError

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|call|execute|vacuum|attach)\b",
    re.IGNORECASE,
)


def check_read_only(sql: str) -> str:
    stripped = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.S).strip().rstrip(";").strip()
    if ";" in stripped:
        raise SourceError("only one statement is allowed")
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        raise SourceError("only SELECT or WITH queries are allowed")
    if _FORBIDDEN.search(stripped):
        raise SourceError("query contains a write or DDL keyword")
    return stripped


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


class SQLSource(DataSource):
    language = "SQL"

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        if not cfg.get("url"):
            raise SourceError(f"source '{name}' needs a 'url' (SQLAlchemy connection string)")
        self.timeout_ms = int(cfg.get("timeout_seconds", 10)) * 1000
        self.engine = create_engine(cfg["url"], pool_pre_ping=True)
        self.dialect = self.engine.dialect.name

    def query(self, q: str) -> list[dict[str, Any]]:
        sql = check_read_only(q)
        try:
            with self.engine.connect() as conn:
                if self.dialect == "postgresql":
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    conn.execute(text(f"SET LOCAL statement_timeout = {self.timeout_ms}"))
                result = conn.execute(text(sql))
                cols = list(result.keys())
                rows = result.fetchmany(self.max_rows)
                conn.rollback()
        except Exception as e:  # surface DB errors to the agent rather than crashing
            raise SourceError(str(e).splitlines()[0]) from e
        return [{c: _jsonable(v) for c, v in zip(cols, r)} for r in rows]

    def close(self) -> None:
        self.engine.dispose()
