"""Distributed traces as a data source: where a request spent its time, and where it failed first.

Reads Grafana Tempo or Jaeger (both take OpenTelemetry). Queries are tiny commands, like the Docker and Kubernetes sources:

  slow <service|*> [minutes] [min_ms] [limit]   the slowest traces, each with the span that took the time (the bottleneck)
  errors <service|*> [minutes] [limit]          traces that failed, each with the span where the failure began and its message
  trace <trace_id>                              one request span by span: who called whom, how long, the span's own time and its share of the request
  graph [minutes]                               who calls whom (services, databases, outside APIs) with call counts, errors, latency
  hotspots <service|*> [minutes]                which operation spends the most time, summed over recent traces, and how many
                                                times each repeats within one request (a high number is an N+1)
  p95 <service> [minutes]                       a number, for checks: the 95th percentile of the service's request spans, in ms
  error_count <service> [minutes]               a number, for checks: how many traces with an error went through the service
  error_pct <service> [minutes]                 a number, for checks: the percentage of recent traces through the service that failed

Only a short allowlist of span attributes is passed on (HTTP method, route and status, database system and statement, peer
service, instance) and URLs lose their query strings, so a trace never carries tokens or user data to the model.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import DataSource, SourceError

_HEX = re.compile(r"^[0-9a-fA-F]{16,32}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.:/\- ]{1,80}$")
KINDS = {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}
_ALLOWED = ("http.method", "http.request.method", "http.route", "http.status_code", "http.response.status_code", "db.system",
            "db.name", "db.statement", "db.query.text", "peer.service", "server.address", "net.peer.name", "http.url", "url.full",
            "service.instance.id", "host.name", "k8s.pod.name", "exception.type", "exception.message")


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str
    service: str
    instance: str
    name: str
    kind: str          # server | client | internal | producer | consumer
    start_ns: int
    dur_ms: float
    error: bool
    message: str
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def end_ns(self) -> int:
        return self.start_ns + int(self.dur_ms * 1e6)


# ---------------------------------------------------------------------------------------------------------------- parsing

def _hex(s: str | None) -> str:
    """Span ids arrive as hex from a search and as base64 from Tempo's trace endpoint."""
    if not s:
        return ""
    if _HEX.match(s):
        return s.lower()
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4)).hex()
    except (binascii.Error, ValueError):
        return s


def _val(v: dict[str, Any]) -> Any:
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return int(v[k]) if k == "intValue" else v[k]
    return None


def _kv(items: list[dict[str, Any]] | None) -> dict[str, Any]:
    return {i["key"]: _val(i.get("value", {})) for i in items or []}


def _safe(attrs: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for k in _ALLOWED:
        if attrs.get(k) not in (None, ""):
            v = attrs[k]
            if k in ("http.url", "url.full"):
                u = urlsplit(str(v))
                v = f"{u.scheme}://{u.netloc}{u.path}"
            out[k] = v if not isinstance(v, str) else v[:240]
    return out


def spans_from_otlp(doc: dict[str, Any]) -> list[Span]:
    """Tempo's trace endpoint: OTLP JSON, `{"trace": {"resourceSpans": [...]}}` (v2) or `{"batches": [...]}` (v1)."""
    trace = doc.get("trace", doc)
    out: list[Span] = []
    for res in trace.get("resourceSpans") or trace.get("batches") or []:
        r = _kv((res.get("resource") or {}).get("attributes"))
        service, instance = str(r.get("service.name", "unknown")), str(r.get("service.instance.id") or r.get("host.name") or "")
        for scope in res.get("scopeSpans") or res.get("instrumentationLibrarySpans") or []:
            for s in scope.get("spans", []):
                a = _kv(s.get("attributes"))
                st = s.get("status") or {}
                err = str(st.get("code", "")).upper() in ("2", "STATUS_CODE_ERROR", "ERROR")
                msg = st.get("message") or ""
                for ev in s.get("events") or []:
                    if ev.get("name") == "exception":
                        ea = _kv(ev.get("attributes"))
                        msg = msg or f"{ea.get('exception.type', '')}: {ea.get('exception.message', '')}".strip(": ")
                        a.setdefault("exception.type", ea.get("exception.type"))
                        a.setdefault("exception.message", ea.get("exception.message"))
                kind = str(s.get("kind", "")).replace("SPAN_KIND_", "").lower()
                kind = {v: k for k, v in KINDS.items()}.get(int(kind), "internal") if kind.isdigit() else (kind or "internal")
                start = int(s.get("startTimeUnixNano", 0))
                out.append(Span(_hex(s.get("traceId")), _hex(s.get("spanId")), _hex(s.get("parentSpanId")), service, instance,
                                s.get("name", ""), kind, start, (int(s.get("endTimeUnixNano", start)) - start) / 1e6, err, str(msg)[:300],
                                _safe({**r, **a})))
    return out


def spans_from_jaeger(trace: dict[str, Any]) -> list[Span]:
    procs = trace.get("processes", {})
    out: list[Span] = []
    for s in trace.get("spans", []):
        tags = {t["key"]: t.get("value") for t in s.get("tags", [])}
        p = procs.get(s.get("processID"), {})
        ptags = {t["key"]: t.get("value") for t in p.get("tags", [])}
        parent = next((r["spanID"] for r in s.get("references", []) if r.get("refType") == "CHILD_OF"), "")
        err = str(tags.get("error", "")).lower() == "true" or str(tags.get("otel.status_code", "")).upper() == "ERROR"
        msg = str(tags.get("otel.status_description") or "")
        for lg in s.get("logs", []):
            f = {x["key"]: x.get("value") for x in lg.get("fields", [])}
            if f.get("event") == "exception" or "exception.message" in f:
                msg = msg or f"{f.get('exception.type', '')}: {f.get('exception.message', '')}".strip(": ")
                tags.setdefault("exception.message", f.get("exception.message"))
        out.append(Span(trace["traceID"], s["spanID"], parent, p.get("serviceName", "unknown"),
                        str(ptags.get("service.instance.id") or ptags.get("hostname") or ptags.get("host.name") or ""),
                        s.get("operationName", ""), str(tags.get("span.kind", "internal")).lower(), int(s.get("startTime", 0)) * 1000,
                        s.get("duration", 0) / 1000.0, err, msg[:300], _safe({**ptags, **tags})))
    return out


# --------------------------------------------------------------------------------------------------------------- analysis

def _children(spans: list[Span]) -> dict[str, list[Span]]:
    kids: dict[str, list[Span]] = {}
    for s in spans:
        kids.setdefault(s.parent_id, []).append(s)
    return kids


def _roots(spans: list[Span]) -> list[Span]:
    ids = {s.span_id for s in spans}
    return sorted((s for s in spans if not s.parent_id or s.parent_id not in ids), key=lambda s: s.start_ns)


def self_times(spans: list[Span]) -> dict[str, float]:
    """A span's own time: its duration minus the time covered by its children (merged, so parallel calls count once)."""
    kids, out = _children(spans), {}
    for s in spans:
        iv = sorted((max(c.start_ns, s.start_ns), min(c.end_ns, s.end_ns)) for c in kids.get(s.span_id, []))
        covered, cur_s, cur_e = 0, None, None
        for a, b in iv:
            if b <= a:
                continue
            if cur_e is None or a > cur_e:
                covered += (cur_e - cur_s) if cur_e is not None else 0
                cur_s, cur_e = a, b
            else:
                cur_e = max(cur_e, b)
        covered += (cur_e - cur_s) if cur_e is not None else 0
        out[s.span_id] = max(0.0, s.dur_ms - covered / 1e6)
    return out


def depths(spans: list[Span]) -> dict[str, int]:
    by_id, out = {s.span_id: s for s in spans}, {}
    for s in spans:
        d, cur, seen = 0, s, set()
        while cur.parent_id in by_id and cur.parent_id not in seen:
            seen.add(cur.parent_id)
            cur = by_id[cur.parent_id]
            d += 1
        out[s.span_id] = d
    return out


def bottleneck(spans: list[Span], selfs: dict[str, float]) -> Span:
    return max(spans, key=lambda s: selfs.get(s.span_id, 0.0))


def failure_origin(spans: list[Span]) -> Span | None:
    """The span where the failure began: an erroring span none of whose children failed, deepest first."""
    errs = [s for s in spans if s.error]
    if not errs:
        return None
    bad_parents = {s.parent_id for s in errs}
    leaves = [s for s in errs if s.span_id not in bad_parents] or errs
    d = depths(spans)
    return max(leaves, key=lambda s: (d.get(s.span_id, 0), s.start_ns))


def _detail(s: Span) -> str:
    a = s.attrs
    stmt = a.get("db.statement") or a.get("db.query.text")
    if stmt:
        return " ".join(str(stmt).split())[:140]
    url = a.get("http.url") or a.get("url.full")
    if url:
        return str(url)
    return str(a.get("peer.service") or a.get("server.address") or a.get("net.peer.name") or "")


def _label(s: Span) -> str:
    return f"{s.service} {s.name}".strip()


def _callee(s: Span) -> str:
    """Who a client span talks to when no span of that service exists: a database or an outside host."""
    a = s.attrs
    if a.get("peer.service"):
        return str(a["peer.service"])
    if a.get("db.system"):
        return f"{a['db.system']}:{a['db.name']}" if a.get("db.name") else str(a["db.system"])
    url = a.get("http.url") or a.get("url.full")
    if url:
        return urlsplit(str(url)).hostname or ""
    return str(a.get("server.address") or a.get("net.peer.name") or "")


def edges(spans: list[Span]) -> list[tuple[str, str, float, bool]]:
    """(caller, callee, ms, failed) for every call in one trace."""
    kids, out = _children(spans), []
    for s in spans:
        if s.kind != "client":
            continue
        nxt = [c for c in kids.get(s.span_id, []) if c.service != s.service]
        callee = nxt[0].service if nxt else _callee(s)
        if callee and callee != s.service:
            out.append((s.service, callee, s.dur_ms, s.error))
    return out


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]


# --------------------------------------------------------------------------------------------------------------- backends

class _Tempo:
    def __init__(self, http: httpx.Client):
        self.http = http

    def _get(self, path: str, **params: Any) -> Any:
        try:
            r = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as e:
            raise SourceError(f"cannot reach Tempo: {e}") from e
        if r.status_code >= 400:
            raise SourceError(f"Tempo returned HTTP {r.status_code}: {r.text[:200]}")
        return r.json() if "json" in r.headers.get("content-type", "") else r.text

    def services(self) -> list[str]:
        data = self._get("/api/search/tag/service.name/values")
        return sorted(data.get("tagValues", [])) if isinstance(data, dict) else []

    def search(self, service: str, minutes: int, min_ms: int = 0, errors: bool = False, limit: int = 50, server_only: bool = False) -> list[dict]:
        cond = []
        if service != "*":
            cond.append(f'resource.service.name = "{service}"')
        if min_ms:
            cond.append(f"duration > {int(min_ms)}ms")
        if errors:
            cond.append("status = error")
        if server_only:
            cond.append("kind = server")
        now = int(time.time())
        data = self._get("/api/search", q="{ " + " && ".join(cond) + " }" if cond else "{}", limit=limit, spss=3,
                         start=now - minutes * 60, end=now + 5)
        out = []
        for t in data.get("traces", []):
            out.append({"trace_id": t["traceID"], "root_service": t.get("rootServiceName", ""), "root_name": t.get("rootTraceName", ""),
                        "start_ns": int(t.get("startTimeUnixNano", 0)), "duration_ms": float(t.get("durationMs", 0)),
                        "matched": [int(s.get("durationNanos", 0)) / 1e6 for ss in (t.get("spanSets") or [t.get("spanSet") or {}])
                                    for s in ss.get("spans", [])]})
        return out

    def trace(self, trace_id: str) -> list[Span]:
        try:
            return spans_from_otlp(self._get(f"/api/v2/traces/{trace_id}"))
        except SourceError:
            return spans_from_otlp(self._get(f"/api/traces/{trace_id}"))


class _Jaeger:
    def __init__(self, http: httpx.Client):
        self.http, self.cache = http, {}

    def _get(self, path: str, **params: Any) -> Any:
        try:
            r = self.http.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.HTTPError as e:
            raise SourceError(f"cannot reach Jaeger: {e}") from e
        if r.status_code >= 400:
            raise SourceError(f"Jaeger returned HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    def services(self) -> list[str]:
        return sorted(s for s in (self._get("/api/services").get("data") or []) if s != "jaeger-query")

    def search(self, service: str, minutes: int, min_ms: int = 0, errors: bool = False, limit: int = 50, server_only: bool = False) -> list[dict]:
        now = int(time.time() * 1e6)
        out = []
        for svc in (self.services()[:10] if service == "*" else [service]):
            data = self._get("/api/traces", service=svc, start=now - minutes * 60 * 10**6, end=now + 5 * 10**6, limit=limit,
                             minDuration=f"{int(min_ms)}ms" if min_ms else None, tags='{"error":"true"}' if errors else None)
            for t in data.get("data", []):
                spans = spans_from_jaeger(t)
                if not spans:
                    continue
                self.cache[t["traceID"]] = spans
                root = _roots(spans)[0]
                start = min(s.start_ns for s in spans)
                mine = [s.dur_ms for s in spans if s.service == svc and (s.kind == "server" or not server_only)]
                out.append({"trace_id": t["traceID"], "root_service": root.service, "root_name": root.name, "start_ns": start,
                            "duration_ms": (max(s.end_ns for s in spans) - start) / 1e6, "matched": mine})
        seen, uniq = set(), []
        for t in sorted(out, key=lambda x: -x["start_ns"]):
            if t["trace_id"] not in seen:
                seen.add(t["trace_id"]); uniq.append(t)
        return uniq[:limit]

    def trace(self, trace_id: str) -> list[Span]:
        if trace_id not in self.cache:
            data = self._get(f"/api/traces/{trace_id}").get("data") or []
            if not data:
                raise SourceError(f"no trace {trace_id}")
            self.cache[trace_id] = spans_from_jaeger(data[0])
        return self.cache[trace_id]


# ------------------------------------------------------------------------------------------------------------------ source

class TracesSource(DataSource):
    language = ("Trace commands (slow <service|*> [minutes] [min_ms] [limit] | errors <service|*> [minutes] [limit] | trace <trace_id> | "
                "graph [minutes] | hotspots <service|*> [minutes] | p95 <service> [minutes] | error_count <service> [minutes] | error_pct <service> [minutes])")

    def __init__(self, name: str, cfg: dict[str, Any]):
        super().__init__(name, cfg)
        url = (cfg.get("url") or "").rstrip("/")
        if not url:
            raise SourceError(f"source '{name}' needs a 'url' (Tempo or Jaeger)")
        headers = {}
        if cfg.get("bearer_token"):
            headers["Authorization"] = f"Bearer {cfg['bearer_token']}"
        if cfg.get("tenant"):
            headers["X-Scope-OrgID"] = str(cfg["tenant"])
        self.http = httpx.Client(base_url=url, headers=headers, timeout=float(cfg.get("timeout_seconds", 10)), verify=cfg.get("verify", True))
        self.kind = cfg.get("kind", "auto")
        self.backend = None  # picked on first use, so building the source never needs the network

    def _be(self):
        if self.backend is None:
            kind = self.kind if self.kind in ("tempo", "jaeger") else detect_kind(self.http)
            self.kind = kind
            self.backend = (_Tempo if kind == "tempo" else _Jaeger)(self.http)
        return self.backend

    @staticmethod
    def _service(token: str) -> str:
        if token != "*" and not _SAFE_NAME.match(token):
            raise SourceError(f"not a service name: {token!r}")
        return token

    @staticmethod
    def _int(token: str | None, default: int, lo: int, hi: int) -> int:
        if token is None:
            return default
        try:
            return max(lo, min(hi, int(float(token))))
        except ValueError:
            raise SourceError(f"expected a number, got {token!r}") from None

    def query(self, q: str) -> list[dict[str, Any]]:
        parts = q.strip().split()
        if not parts:
            raise SourceError("empty query. " + self.language)
        cmd, args = parts[0].lower(), parts[1:]
        try:
            if cmd == "slow":
                return self._slow(self._service(args[0]), self._int(args[1] if len(args) > 1 else None, 15, 1, 1440),
                                  self._int(args[2] if len(args) > 2 else None, 0, 0, 600000), self._int(args[3] if len(args) > 3 else None, 8, 1, 20))
            if cmd == "errors":
                return self._errors(self._service(args[0]), self._int(args[1] if len(args) > 1 else None, 15, 1, 1440),
                                    self._int(args[2] if len(args) > 2 else None, 8, 1, 20))
            if cmd == "trace":
                if not args or not _HEX.match(args[0]):
                    raise SourceError("trace needs a trace id (hex), e.g. `trace 86cdaf9370d10f94a2c75c7bd8a8639a`")
                return self._trace(args[0].lower())
            if cmd == "graph":
                return self._graph(self._int(args[0] if args else None, 15, 1, 1440))
            if cmd == "hotspots":
                return self._hotspots(self._service(args[0]), self._int(args[1] if len(args) > 1 else None, 15, 1, 1440))
            if cmd == "p95":
                return self._p95(self._service(args[0]), self._int(args[1] if len(args) > 1 else None, 5, 1, 1440))
            if cmd == "error_pct":
                return self._error_pct(self._service(args[0]), self._int(args[1] if len(args) > 1 else None, 2, 1, 1440))
            if cmd == "error_count":
                return self._error_count(self._service(args[0]), self._int(args[1] if len(args) > 1 else None, 2, 1, 1440))
        except IndexError:
            raise SourceError(f"`{cmd}` needs a service name. " + self.language) from None
        raise SourceError(f"unknown command '{cmd}'. " + self.language)

    def ping(self) -> None:
        self._be().services()

    # -- commands ---------------------------------------------------------------------------------

    def _slow(self, svc: str, minutes: int, min_ms: int, limit: int) -> list[dict[str, Any]]:
        be = self._be()
        found = sorted(be.search(svc, minutes, min_ms=min_ms, limit=100), key=lambda t: -t["duration_ms"])[:limit]
        rows = []
        for t in found:
            spans = be.trace(t["trace_id"])
            if not spans:
                continue
            st = self_times(spans)
            b = bottleneck(spans, st)
            origin = failure_origin(spans)
            rows.append({"trace_id": t["trace_id"], "root": f"{t['root_service']} {t['root_name']}".strip(), "duration_ms": round(t["duration_ms"]),
                         "spans": len(spans), "failed": bool(origin),
                         "bottleneck": f"{_label(b)} ({b.instance})" if b.instance and b.instance != b.service else _label(b),
                         "bottleneck_ms": round(st[b.span_id]), "bottleneck_detail": _detail(b)})
        return rows[: self.max_rows]

    def _errors(self, svc: str, minutes: int, limit: int) -> list[dict[str, Any]]:
        be = self._be()
        found = sorted(be.search(svc, minutes, errors=True, limit=100), key=lambda t: -t["start_ns"])[:limit]
        rows = []
        for t in found:
            spans = be.trace(t["trace_id"])
            o = failure_origin(spans)
            if o is None:
                continue
            code = o.attrs.get("http.status_code") or o.attrs.get("http.response.status_code")
            rows.append({"trace_id": t["trace_id"], "root": f"{t['root_service']} {t['root_name']}".strip(), "duration_ms": round(t["duration_ms"]),
                         "failed_at": _label(o), "instance": o.instance, "message": o.message or o.attrs.get("exception.message") or "",
                         "http_status": code, "detail": _detail(o)})
        return rows[: self.max_rows]

    def _trace(self, trace_id: str) -> list[dict[str, Any]]:
        spans = self._be().trace(trace_id)
        if not spans:
            raise SourceError(f"no spans for trace {trace_id}")
        st, dp = self_times(spans), depths(spans)
        t0 = min(s.start_ns for s in spans)
        total = max(max(s.end_ns for s in spans) - t0, 1) / 1e6
        rows = []
        for s in sorted(spans, key=lambda s: (s.start_ns, dp[s.span_id])):
            rows.append({"depth": dp[s.span_id], "service": s.service, "instance": s.instance, "operation": s.name, "kind": s.kind,
                         "start_ms": round((s.start_ns - t0) / 1e6, 1), "duration_ms": round(s.dur_ms, 1), "self_ms": round(st[s.span_id], 1),
                         "share_of_request_pct": round(100 * st[s.span_id] / total), "status": "error" if s.error else "ok", "detail": _detail(s), "error": s.message})
        return rows[: max(self.max_rows, 100)]

    def _sample(self, svc: str, minutes: int, n: int) -> list[list[Span]]:
        be = self._be()
        return [be.trace(t["trace_id"]) for t in be.search(svc, minutes, limit=n)]

    def _graph(self, minutes: int) -> list[dict[str, Any]]:
        agg: dict[tuple[str, str], list[tuple[float, bool]]] = {}
        for spans in self._sample("*", minutes, 40):
            for a, b, ms, bad in edges(spans):
                agg.setdefault((a, b), []).append((ms, bad))
        rows = [{"caller": a, "callee": b, "calls": len(v), "errors": sum(1 for _, bad in v if bad),
                 "p50_ms": round(_pct([m for m, _ in v], .5), 1), "p95_ms": round(_pct([m for m, _ in v], .95), 1)} for (a, b), v in agg.items()]
        return sorted(rows, key=lambda r: -r["calls"])[: self.max_rows]

    def _hotspots(self, svc: str, minutes: int) -> list[dict[str, Any]]:
        agg: dict[tuple[str, str], dict[str, Any]] = {}
        for spans in self._sample(svc, minutes, 30):
            st = self_times(spans)
            seen_here: dict[tuple[str, str], int] = {}
            for s in spans:
                key = (s.service, s.name)
                a = agg.setdefault(key, {"service": s.service, "operation": s.name, "spans": 0, "traces": 0, "most_in_one": 0, "total": 0.0,
                                         "max": 0.0, "trace": s.trace_id, "detail": _detail(s)})
                a["spans"] += 1
                a["total"] += st[s.span_id]
                seen_here[key] = seen_here.get(key, 0) + 1
                if st[s.span_id] > a["max"]:
                    a["max"], a["trace"], a["detail"] = st[s.span_id], s.trace_id, _detail(s)
            for key, n in seen_here.items():
                agg[key]["traces"] += 1
                agg[key]["most_in_one"] = max(agg[key]["most_in_one"], n)
        rows = []
        for a in agg.values():
            per = round(a["spans"] / max(a["traces"], 1), 1)
            row = {"service": a["service"], "operation": a["operation"], "spans": a["spans"], "traces": a["traces"],
                   "repeats_per_request": per, "total_self_ms": round(a["total"]), "avg_self_ms": round(a["total"] / a["spans"], 1),
                   "max_self_ms": round(a["max"]), "example_trace": a["trace"], "detail": a["detail"]}
            if per >= 5:  # one request making the same call over and over is the signature of an N+1
                row["note"] = f"called about {per:g} times in each request that makes it: an N+1 pattern if these are database queries"
            rows.append(row)
        return sorted(rows, key=lambda r: -r["total_self_ms"])[:8]

    def _p95(self, svc: str, minutes: int) -> list[dict[str, Any]]:
        be = self._be()
        found = be.search(svc, minutes, limit=100, server_only=True)
        values = [m for t in found for m in t["matched"]]
        return [{"value": round(_pct(values, .95), 1)}] if values else []

    def _error_count(self, svc: str, minutes: int) -> list[dict[str, Any]]:
        found = self._be().search(svc, minutes, errors=True, limit=200)
        return [{"value": float(len(found))}] if found or self._be().search(svc, minutes, limit=1) else []

    def _error_pct(self, svc: str, minutes: int) -> list[dict[str, Any]]:
        """Failed share of the newest traces. Fewer than 20 requests are counted as 20, so a quiet service does not alert on
        one unlucky request."""
        be = self._be()
        sample = {t["trace_id"] for t in be.search(svc, minutes, limit=200)}  # the newest traces, counted whole
        if not sample:
            return []
        failed = sum(1 for t in be.search(svc, minutes, errors=True, limit=200) if t["trace_id"] in sample)  # same traces only
        return [{"value": round(100.0 * failed / max(len(sample), 20), 1)}]

    def close(self) -> None:
        self.http.close()


def detect_kind(http: httpx.Client) -> str:
    """Tempo answers /api/echo; Jaeger's query service answers /api/services with {"data": [...]}."""
    try:
        r = http.get("/api/echo")
        if r.status_code == 200 and "echo" in r.text:
            return "tempo"
    except httpx.HTTPError as e:
        raise SourceError(f"cannot reach the trace store: {e}") from e
    try:
        r = http.get("/api/services")
        if r.status_code == 200 and "data" in r.json():
            return "jaeger"
    except (httpx.HTTPError, ValueError):
        pass
    raise SourceError("the address answers neither as Grafana Tempo (/api/echo) nor as Jaeger (/api/services)")
