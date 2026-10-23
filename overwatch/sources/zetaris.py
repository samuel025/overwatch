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

import hashlib
import math
import os
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from .base import DataSource, SourceError
from .sql import check_read_only

_META = re.compile(r"^(show|describe|desc)\b", re.IGNORECASE)
_REST_COMMAND = re.compile(r"^(promql|logql)\s+(.+)$", re.IGNORECASE | re.DOTALL)
_REST_DB = "ow_rest"      # Lightning database holding the raw REST tables (one per distinct request)
_REST_VIEWS = "ow_views"  # SchemaStore container holding the flattened views the Investigator queries

LANGUAGE = (
    "Zetaris Lightning SQL (Spark SQL). Tables are <datasource>.<table>, e.g. shopdb.orders; a query may join "
    "tables from different datasources. Time windows: current_timestamp() - INTERVAL 5 MINUTES. Use backticks "
    "around odd identifiers. Discover with SHOW DATASOURCES."
)


REST_LANGUAGE = (
    " Live monitoring data is also reachable here: `promql <expression>` runs a PromQL instant query on Prometheus "
    "and returns rows (metric = map of labels, e.g. metric['route'], plus ts and value); `logql <expression>` "
    "runs a LogQL query on Loki and returns rows (labels map, ts_ns, line)."
)


def reachable(url: str) -> str:
    """Zetaris runs in Docker: `localhost` in a URL the user gave means the host, which it reaches as ZETARIS_HOST_ALIAS."""
    alias = os.environ.get("ZETARIS_HOST_ALIAS", "")
    parts = urlsplit(url)
    if alias and parts.hostname in ("localhost", "127.0.0.1", "::1"):
        netloc = alias + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return url


def _jsonable(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None  # NaN/Infinity are not valid JSON
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
        raw_rest = cfg.get("rest") or {}  # {"prometheus": url, "loki": url, "loki_headers": {...}}
        self.rest: dict[str, Any] = {k: reachable(v) if k in ("prometheus", "loki") and v else v for k, v in raw_rest.items()}
        self._rest_ready: set[str] = set()
        self._scaffold = False
        if self.rest.get("prometheus") or self.rest.get("loki"):
            self.language = LANGUAGE + REST_LANGUAGE

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

    def _plain(self, sql: str) -> None:
        """Run a DDL statement without a prepared statement: a `?` in a URL is not a placeholder."""
        conn = self._connect()
        try:
            st = conn.jconn.createStatement()
            try:
                st.execute(sql)
            finally:
                st.close()
        except Exception as e:
            raise SourceError(" ".join(str(e).split())[:400]) from e

    def _ensure_once(self, sql: str) -> None:
        try:
            self._plain(sql)
        except SourceError as e:
            if "already exist" not in str(e).lower():
                raise

    def _rest_view(self, kind: str, expr: str) -> str | None:
        """Make Zetaris read `expr` from Prometheus/Loki as a table, flattened by a view that fits the shape of the
        answer. Overwatch writes the DDL itself; the expression is percent-encoded, so nothing an agent sends can
        alter the statement. Returns None for an empty answer (there is nothing to flatten, and Zetaris cannot
        type an empty list), so the next ask starts again with fresh type inference."""
        base = (self.rest.get("prometheus" if kind == "promql" else "loki") or "").rstrip("/")
        if not base:
            raise SourceError(f"{kind} is not configured: no {'Prometheus' if kind == 'promql' else 'Loki'} was connected")
        digest = hashlib.sha1(f"{kind}:{base}:{expr}".encode()).hexdigest()[:10]
        raw, view = f"{kind}_{digest}_raw", f"{kind}_{digest}"
        if view in self._rest_ready:
            return view
        if not self._scaffold:
            self._ensure_once(f'CREATE LIGHTNING DATABASE {_REST_DB} DESCRIBE BY "Overwatch live monitoring data"')
            self._ensure_once(f"CREATE SCHEMASTORE CONTAINER {_REST_VIEWS}")
            self._scaffold = True
        if kind == "promql":
            endpoint, headers = f"{base}/api/v1/query?query={quote(expr, safe='')}", ""
        else:
            endpoint = f"{base}/loki/api/v1/query_range?query={quote(expr, safe='')}&limit={self.max_rows}&direction=backward"
            headers = " ".join(f'{k} "{v}"' for k, v in (self.rest.get("loki_headers") or {}).items())
        self._ensure_once(f'CREATE LIGHTNING REST TABLE {raw} FROM {_REST_DB} REQUEST(endpoint "{endpoint}", '
                          f'method "get", response_type "json", http_encoding "URLENCODED") HEADER ({headers}) BODY ()')
        shape = self._run(f"SELECT status, data.resultType AS kind, size(data.result) AS n FROM {_REST_DB}.{raw}", 1)
        if not shape or shape[0].get("status") != "success":
            self._drop_raw(raw)
            raise SourceError(f"{'Prometheus' if kind == 'promql' else 'Loki'} did not accept the query")
        if not shape[0].get("n"):
            self._drop_raw(raw)  # empty answer: nothing to flatten; a later answer may have rows of a real type
            return None
        t, src = shape[0]["kind"], f"{_REST_DB}.{raw}"
        # labels may be absent (an empty label set has no field in the inferred schema): read them out of the JSON
        metric = "from_json(get_json_object(to_json(r), '$.metric'), 'map<string,string>')"
        stream = "from_json(to_json(s.stream), 'map<string,string>')"
        if t == "streams":
            select = (f"SELECT {stream} AS labels, v[0] AS ts_ns, v[1] AS line FROM {src} "
                      "LATERAL VIEW explode(data.result) AS s LATERAL VIEW explode(s.values) AS v")
        elif t == "matrix":
            select = (f"SELECT {metric} AS metric, CAST(v[0] AS DOUBLE) AS ts, CAST(v[1] AS DOUBLE) AS value "
                      f"FROM {src} LATERAL VIEW explode(data.result) AS r LATERAL VIEW explode(r.values) AS v")
        elif t in ("scalar", "string"):
            select = f"SELECT CAST(data.result[0] AS DOUBLE) AS ts, data.result[1] AS value FROM {src}"
        else:  # vector
            select = (f"SELECT {metric} AS metric, CAST(r.value[0] AS DOUBLE) AS ts, CAST(r.value[1] AS DOUBLE) AS value "
                      f"FROM {src} LATERAL VIEW explode(data.result) AS r")
        self._plain(f"CREATE OR REPLACE SCHEMASTORE VIEW {view} WITH CONTAINER {_REST_VIEWS} AS {select}")
        self._rest_ready.add(view)
        return view

    def _drop_raw(self, raw: str) -> None:
        try:
            self._plain(f"DROP LIGHTNING DATABASE TABLE {_REST_DB}.{raw}")
        except SourceError:
            pass

    def query(self, q: str) -> list[dict[str, Any]]:
        stripped = q.strip().rstrip(";").strip()
        if m := _REST_COMMAND.match(stripped):
            view = self._rest_view(m.group(1).lower(), m.group(2).strip())
            return self._run(f"SELECT * FROM {_REST_VIEWS}.{view}", self.max_rows) if view else []
        sql = stripped if _META.match(stripped) and ";" not in stripped else check_read_only(q)
        return self._run(sql, self.max_rows)

    def admin(self, statements: list[str]) -> list[str]:
        """Run registration statements (CREATE DATASOURCE ...), one at a time. Used by Overwatch's own setup
        flow with the deployment's admin account; never reachable by agents (they only call query())."""
        done = []
        for s in statements:
            if s.upper().startswith("CREATE DATASOURCE "):  # re-running setup must replace, not fail "already exists"
                try:
                    self._run("DROP DATASOURCE " + s.split()[2], None)
                except SourceError:
                    pass
            self._run(s, None)
            done.append(s.split("(")[0].strip()[:80])
        return done

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
