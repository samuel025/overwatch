"""The traces source against real Tempo and Jaeger responses (tests/fixtures), plus the rules that keep it safe."""

import json
from pathlib import Path

import httpx
import pytest

from overwatch.sources.base import SourceError
from overwatch.sources.traces import TracesSource, detect_kind, depths, failure_origin, self_times, spans_from_jaeger, spans_from_otlp

FIX = Path(__file__).parent / "fixtures"
load = lambda p: json.loads((FIX / p).read_text())  # noqa: E731
SLOW_IDS = {t["traceID"] for t in load("tempo/search_slow.json")["traces"]}


def make(kind, handler):
    src = TracesSource("traces", {"url": "http://store", "kind": kind})
    src.http = httpx.Client(base_url="http://store", transport=httpx.MockTransport(handler))
    return src


def tempo_handler(seen: list | None = None):
    def h(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(dict(request.url.params))
        p = request.url.path
        if p == "/api/echo":
            return httpx.Response(200, text="echo")
        if p == "/api/search/tag/service.name/values":
            return httpx.Response(200, json=load("tempo/services.json"))
        if p == "/api/search":
            q = request.url.params.get("q", "")
            f = "search_errors" if "status = error" in q else "search_slow" if "duration >" in q else "search_all"
            return httpx.Response(200, json=load(f"tempo/{f}.json"))
        if p.startswith("/api/v2/traces/"):
            tid = p.rsplit("/", 1)[1]
            return httpx.Response(200, json=load("tempo/trace_v2.json" if tid in SLOW_IDS or tid not in
                                                   {t["traceID"] for t in load("tempo/search_errors.json")["traces"]} else "tempo/trace_v2_error.json"))
        return httpx.Response(404)
    return h


def jaeger_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    if p == "/api/echo":
        return httpx.Response(404)
    if p == "/api/services":
        return httpx.Response(200, json=load("jaeger/services.json"))
    if p == "/api/traces":
        errors = "error" in request.url.params.get("tags", "")
        return httpx.Response(200, json=load("jaeger/traces_payments_errors.json" if errors else "jaeger/traces_orders.json"))
    return httpx.Response(404)


# ---------------------------------------------------------------------------------------------------------------- parsing

def test_tempo_trace_ids_arrive_as_base64_and_the_tree_is_rebuilt_from_them():
    spans = spans_from_otlp(load("tempo/trace_v2.json"))
    assert len(spans) == 7 and all(len(s.span_id) == 16 and len(s.trace_id) == 32 for s in spans)
    assert all(int(s.span_id, 16) >= 0 for s in spans)                       # real hex now
    root = next(s for s in spans if not s.parent_id)
    assert root.service == "gateway" and root.kind == "server" and not root.error
    by_id = {s.span_id for s in spans}
    assert all(s.parent_id in by_id for s in spans if s.parent_id)           # every parent resolves: base64 was decoded consistently
    assert max(depths(spans).values()) == 3


def test_self_time_shows_where_the_time_was_spent_not_just_how_long_a_span_took():
    spans = spans_from_otlp(load("tempo/trace_v2.json"))
    st = self_times(spans)
    db = next(s for s in spans if s.name == "SELECT shopdb.orders")
    parent = next(s for s in spans if s.name == "GET /orders")
    assert st[db.span_id] > 890                                              # the query's time is its own
    assert st[parent.span_id] < 10                                           # the service around it only waited


def test_the_failure_origin_is_the_deepest_failing_span_not_the_gateway_that_merely_reported_it():
    spans = spans_from_otlp(load("tempo/trace_v2_error.json"))
    assert sum(s.error for s in spans) >= 3                                  # the error bubbled up through three spans
    o = failure_origin(spans)
    assert (o.service, o.name) == ("payments", "POST psp.example.com") and "502" in o.message


def test_only_allowlisted_attributes_pass_and_urls_lose_their_query_strings():
    doc = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "api"}}]},
                              "scopeSpans": [{"spans": [{"traceId": "a" * 32, "spanId": "b" * 16, "name": "GET /x", "kind": 3,
                                                         "startTimeUnixNano": "1", "endTimeUnixNano": "2000001",
                                                         "attributes": [
                                                             {"key": "http.url", "value": {"stringValue": "https://h.example/v1/pay?token=SECRET&user=jo"}},
                                                             {"key": "http.request.header.authorization", "value": {"stringValue": "Bearer SECRET"}},
                                                             {"key": "enduser.id", "value": {"stringValue": "jo@example.com"}}]}]}]}]}
    (s,) = spans_from_otlp(doc)
    assert s.attrs == {"http.url": "https://h.example/v1/pay"} and s.kind == "client"
    assert "SECRET" not in json.dumps(s.attrs)


def test_jaeger_spans_normalise_to_the_same_shape():
    t = load("jaeger/traces_payments_errors.json")["data"][0]
    spans = spans_from_jaeger(t)
    o = failure_origin(spans)
    assert (o.service, o.name) == ("payments", "POST psp.example.com") and o.kind == "client"
    assert len(spans) == 7 and {s.service for s in spans} == {"gateway", "payments"} or len({s.service for s in spans}) >= 2


# ------------------------------------------------------------------------------------------------------------------ Tempo

def test_slow_names_the_bottleneck_span_and_its_query():
    rows = make("tempo", tempo_handler()).query("slow orders 15 500 3")
    assert len(rows) == 3 and rows[0]["duration_ms"] >= rows[-1]["duration_ms"]
    top = rows[0]
    assert top["bottleneck"] == "orders SELECT shopdb.orders" and top["bottleneck_ms"] >= 890
    assert top["bottleneck_detail"].startswith("SELECT * FROM orders WHERE customer_id")


def test_errors_name_the_span_where_the_failure_began():
    rows = make("tempo", tempo_handler()).query("errors * 15 5")
    assert rows and all(r["failed_at"] == "payments POST psp.example.com" for r in rows)
    assert "502" in rows[0]["message"] and rows[0]["instance"].startswith("payments-")


def test_trace_is_a_readable_waterfall_with_each_spans_share_of_the_request():
    src = make("tempo", tempo_handler())
    rows = src.query(f"trace {next(iter(SLOW_IDS))}")
    assert [r["depth"] for r in rows] == [0, 1, 2, 3, 1, 2, 3]
    db = next(r for r in rows if r["operation"] == "SELECT shopdb.orders")
    assert db["share_of_request_pct"] >= 85 and db["self_ms"] >= 890


def test_graph_rebuilds_who_calls_whom_including_the_database_and_the_outside_api():
    rows = make("tempo", tempo_handler()).query("graph 15")
    pairs = {(r["caller"], r["callee"]) for r in rows}
    assert {("gateway", "orders"), ("gateway", "payments"), ("orders", "postgresql:shopdb"), ("payments", "psp.example.com")} <= pairs


def test_hotspots_rank_operations_by_the_time_they_really_spend():
    rows = make("tempo", tempo_handler()).query("hotspots * 15")
    assert rows[0]["operation"] == "SELECT shopdb.orders" and rows[0]["total_self_ms"] > rows[1]["total_self_ms"]


def test_the_numbers_checks_need_are_plain_values_and_empty_when_there_is_no_data():
    src = make("tempo", tempo_handler())
    assert src.query("p95 orders 5")[0]["value"] > 0
    assert src.query("error_count payments 2")[0]["value"] >= 1
    empty = make("tempo", lambda r: httpx.Response(200, json={"traces": []}) if r.url.path == "/api/search" else httpx.Response(200, text="echo"))
    assert empty.query("p95 orders 5") == [] and empty.query("error_count orders 2") == []   # a quiet store is "no data", not zero


def test_the_traceql_sent_to_tempo_carries_the_service_duration_and_error_filters():
    seen: list = []
    make("tempo", tempo_handler(seen)).query("slow orders 15 500 3")
    q = next(p["q"] for p in seen if "q" in p)
    assert q == '{ resource.service.name = "orders" && duration > 500ms }'
    seen.clear()
    make("tempo", tempo_handler(seen)).query("errors payments 10 3")
    assert next(p["q"] for p in seen if "q" in p) == '{ resource.service.name = "payments" && status = error }'


# ----------------------------------------------------------------------------------------------------------------- Jaeger

def test_jaeger_gives_the_same_answers():
    src = make("jaeger", jaeger_handler)
    slow = src.query("slow orders 15 500 2")
    assert slow[0]["bottleneck"] == "orders SELECT shopdb.orders" and slow[0]["bottleneck_ms"] >= 890
    err = src.query("errors payments 15 3")
    assert err and err[0]["failed_at"] == "payments POST psp.example.com"
    assert src.query("error_count payments 15")[0]["value"] == 3.0
    assert 0 < src.query("error_pct payments 15")[0]["value"] <= 100
    pairs = {(r["caller"], r["callee"]) for r in src.query("graph 15")}
    assert ("orders", "postgresql:shopdb") in pairs and ("payments", "psp.example.com") in pairs


# ------------------------------------------------------------------------------------------------- detection and validation

def test_the_backend_is_told_apart_by_what_it_answers():
    assert detect_kind(httpx.Client(base_url="http://x", transport=httpx.MockTransport(tempo_handler()))) == "tempo"
    assert detect_kind(httpx.Client(base_url="http://x", transport=httpx.MockTransport(jaeger_handler))) == "jaeger"
    with pytest.raises(SourceError, match="neither"):
        detect_kind(httpx.Client(base_url="http://x", transport=httpx.MockTransport(lambda r: httpx.Response(404))))


@pytest.mark.parametrize("q", ['slow orders" || true || "x', "slow a;b", "trace not-a-hex-id", "trace ../../etc/passwd", "slow", "frobnicate x", ""])
def test_nothing_an_agent_writes_can_reach_the_trace_store_unchecked(q):
    called = []
    src = make("tempo", lambda r: called.append(r) or httpx.Response(200, json={"traces": []}))
    with pytest.raises(SourceError):
        src.query(q)
    assert not called                                                        # rejected before any request was made


# ------------------------------------------------------------------------------------------------- doctor's probe (ping)

def test_a_source_that_does_not_speak_sql_is_probed_in_its_own_language_not_with_select_1():
    from overwatch.sources.base import DataSource
    from overwatch.sources.kubernetes import KubernetesSource
    from overwatch.sources.loki import LokiSource

    asked = []

    class Sql(DataSource):
        language = "SQL"

        def query(self, q):
            asked.append(q)
            return []

    Sql("db", {}).ping()
    assert asked == ["SELECT 1"]                                              # the default stays what it was

    k = KubernetesSource("k8s", {"url": "https://api"})
    k.pods = lambda *a, **kw: asked.append("pods") or []
    k.ping()
    lk = LokiSource("loki", {"url": "http://loki"})
    lk.labels = lambda: asked.append("labels") or []
    lk.ping()
    t = make("tempo", tempo_handler())
    t.ping()                                                                  # reaches Tempo, detects it, asks for its services
    assert asked[1:] == ["pods", "labels"] and t.kind == "tempo"


def test_doctor_reports_a_traces_source_as_ok_when_it_answers_and_failed_when_it_does_not(capsys):
    from overwatch import cli

    class Ow:
        class cfg:
            system_name = "t"
            llm = type("L", (), {"base_url": "", "investigator_model": ""})()
            checks, topology, notifiers = [], {}, []
        sources = {"traces": make("tempo", tempo_handler()), "broken": make("tempo", lambda r: httpx.Response(500, text="boom"))}
        llm = type("M", (), {"health": lambda self: []})()
        memory = type("Mem", (), {"name": "none", "health": lambda self: {"ok": True}})()

    try:
        cli.cmd_doctor(Ow())
    except Exception:
        pass  # the later sections need a full Overwatch; only the Sources lines matter here
    out = capsys.readouterr().out
    assert "[ok] traces" in out and "[FAIL] broken" in out


def test_hotspots_flag_a_call_repeated_many_times_in_one_request_as_an_n_plus_one():
    from overwatch.sources.traces import Span

    def trace(tid, n):
        sp = [Span(tid, "r" * 16, "", "gateway", "g", "GET /checkout", "server", 0, 1000, False, ""),
              Span(tid, "o" * 16, "r" * 16, "orders", "o", "GET /orders", "server", 1_000_000, 900, False, ""),
              Span(tid, "i" * 16, "o" * 16, "orders", "o", "SELECT ids", "client", 2_000_000, 5, False, "", {"db.statement": "SELECT id FROM orders"})]
        for k in range(n):
            sp.append(Span(tid, f"{k:016x}", "o" * 16, "orders", "o", "SELECT by id", "client", 10_000_000 + k * 40_000_000, 40, False, "",
                           {"db.statement": "SELECT * FROM orders WHERE id = $1"}))
        return sp

    src = make("tempo", tempo_handler())
    src._sample = lambda svc, minutes, n: [trace(f"{i:032x}", 20) for i in range(3)]
    rows = {r["operation"]: r for r in src.query("hotspots orders 5")}
    assert rows["SELECT by id"]["repeats_per_request"] == 20.0 and "N+1" in rows["SELECT by id"]["note"]
    assert rows["SELECT by id"]["traces"] == 3 and rows["SELECT by id"]["spans"] == 60
    assert "note" not in rows["SELECT ids"] and rows["SELECT ids"]["repeats_per_request"] == 1.0   # a one-off query is not flagged
    assert max(rows.values(), key=lambda r: r["total_self_ms"])["operation"] == "SELECT by id"      # and it is the biggest time sink


def test_a_trace_service_that_docker_runs_as_replicas_is_one_node_not_a_stranger_beside_them(monkeypatch):
    from overwatch import discovery

    tr = {"url": "http://t", "kind": "tempo", "source": {"type": "traces"}, "catalog": "", "services": ["gateway", "payments"],
          "topology": {"gateway": ["payments"]}, "checks": [{"name": "payments_trace_p95_ms", "service": "payments"}],
          "evidence": [], "log_hits": {"payments": {"psp.example.com": 6}}, "internal": [], "databases": {}}
    monkeypatch.setattr(discovery, "discover_traces", lambda *a, **k: tr)
    monkeypatch.setattr(discovery, "discover_docker", lambda *a, **k: {
        "services": {"gateway": {}, "payments-a": {}, "payments-b": {}}, "topology": {}, "checks": [], "evidence": [], "notes": [],
        "catalog": "", "log_hits": {}, "internal": [], "runtime_facts": "", "log_source": None, "project": "x",
        "projects": ["x"], "service_label": "l", "env_hits": {}})
    plan = discovery.discover(docker={"project": "x"}, traces={"url": "http://t"})
    assert "payments" not in plan["services"]
    assert plan["topology"]["gateway"] == ["payments-a", "payments-b"]
    mine = {c["service"]: c for c in plan["checks"] if c["name"].endswith("trace_p95_ms")}
    assert set(mine) == {"payments-a", "payments-b"}   # neither replica is singled out as the one the check measures
    assert mine["payments-b"]["name"] == "payments_b_trace_p95_ms" and any("payments-b" in n for n in plan["notes"])
    assert all("payments" not in e["callers"] for e in plan["externals"]) and plan["externals"][0]["callers"] == ["payments-a", "payments-b"]
    assert discovery.replica_group("payments", {"payments"}) == [] and discovery.replica_group("pay", {"payments-a"}) == []


def test_a_few_stray_errors_in_busy_traffic_stay_under_the_error_share_check_but_a_failing_service_does_not(monkeypatch):
    src = make("tempo", tempo_handler())

    class Be:
        def __init__(self, total, failed):
            self.total, self.failed = total, failed

        def search(self, svc, minutes, errors=False, limit=100, **kw):
            n = self.failed if errors else self.total
            return [{"trace_id": f"t{i}"} for i in range(n)]

    for total, failed, expect in [(200, 2, 1.0), (200, 70, 35.0), (4, 1, 5.0), (4, 4, 20.0)]:
        monkeypatch.setattr(src, "_be", lambda b=Be(total, failed): b)
        assert src.query("error_pct payments 2")[0]["value"] == expect
    class Busy(Be):  # far more failing traces exist than the 200 newest: only those inside the sample count
        def search(self, svc, minutes, errors=False, limit=100, **kw):
            return [{"trace_id": f"t{i}"} for i in range(limit if not errors else 500)][:limit]

    monkeypatch.setattr(src, "_be", lambda: Busy(0, 0))
    assert src.query("error_pct payments 2")[0]["value"] == 100.0
    monkeypatch.setattr(src, "_be", lambda: Be(0, 0))
    assert src.query("error_pct payments 2") == []
