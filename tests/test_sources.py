"""Loki and Kubernetes sources against recorded-style API responses (no cluster needed)."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from overwatch.sources import build_sources
from overwatch.sources.base import SourceError
from overwatch.sources.kubernetes import KubernetesSource
from overwatch.sources.loki import LokiSource


def _client(handler, base="http://x"):
    return httpx.Client(base_url=base, transport=httpx.MockTransport(handler))


def test_loki_log_lines_and_counts():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/loki/api/v1/query_range":
            return httpx.Response(200, json={"data": {"result": [{"stream": {"app": "payments"}, "values": [
                ["1767225600000000000", "ERROR card declined"], ["1767225601000000000", "ok"]]}]}})
        return httpx.Response(200, json={"data": {"result": [{"metric": {}, "value": [1767225600, "7"]}]}})

    src = LokiSource("loki", {"url": "http://loki"})
    src.client = _client(handler)
    rows = src.query('{app="payments"} |= "error"')
    assert [r["line"] for r in rows] == ["ERROR card declined", "ok"] and rows[0]["app"] == "payments"
    assert src.query('sum(count_over_time({app="payments"} |= "error" [2m]))') == [{"value": 7.0}]
    assert seen == ["/loki/api/v1/query_range", "/loki/api/v1/query"]


def test_loki_empty_result_means_no_rows_and_errors_are_clear():
    src = LokiSource("loki", {"url": "http://loki"})
    src.client = _client(lambda r: httpx.Response(200, json={"data": {"result": []}}))
    assert src.query("sum(rate({app=\"x\"}[1m]))") == []
    src.client = _client(lambda r: httpx.Response(400, text="parse error"))
    with pytest.raises(SourceError, match="HTTP 400"):
        src.query('{bad')


def _pod(name, service, phase="Running", ready=True, restarts=0, last=None):
    cs = {"ready": ready, "restartCount": restarts, "state": {}}
    if last:
        cs["lastState"] = {"terminated": last}
    return {"metadata": {"name": name, "labels": {"app": service}}, "spec": {"nodeName": "n1", "containers": [{"image": "img:1"}]},
            "status": {"phase": phase, "startTime": "2026-10-23T10:00:00Z", "containerStatuses": [cs]}}


def _k8s(routes):
    def handler(req: httpx.Request) -> httpx.Response:
        for suffix, body in routes.items():
            if req.url.path.endswith(suffix):
                if isinstance(body, str):
                    return httpx.Response(200, text=body, headers={"content-type": "text/plain"})
                return httpx.Response(200, json=body)
        return httpx.Response(404, text="nope")

    src = KubernetesSource("k8s", {"url": "http://api", "namespace": "shop"})
    src.client = _client(handler)
    return src


def test_kubernetes_pods_oom_and_running_count():
    oom = {"reason": "OOMKilled", "exitCode": 137, "finishedAt": "2026-10-23T10:05:00Z"}
    src = _k8s({"/pods": {"items": [_pod("orders-1", "orders", restarts=3, last=oom), _pod("pay-1", "payments"),
                                    _pod("pay-2", "payments", phase="Pending", ready=False)]}})
    rows = src.query("pods")
    orders = next(r for r in rows if r["pod"] == "orders-1")
    assert orders["last_exit_reason"] == "OOMKilled" and orders["restarts"] == 3 and orders["service"] == "orders"
    assert src.running("payments") == 1 and src.running("orders") == 1 and src.running("nothing") == 0


def test_kubernetes_logs_errors_count_and_unknown_service():
    now = datetime.now(timezone.utc)
    log = f"{now.isoformat()} INFO started\n{now.isoformat()} ERROR psp timeout\n"
    src = _k8s({"/pods": {"items": [_pod("pay-1", "payments")]}, "/log": log})
    assert [r["line"] for r in src.query("logs payments")] == ["INFO started", "ERROR psp timeout"]
    assert [r["line"] for r in src.query("errors payments")] == ["ERROR psp timeout"]
    assert src.query("count * 2 psp") == [{"value": 1}]
    with pytest.raises(SourceError, match="Known services: payments"):
        src.query("logs ghost")


def test_kubernetes_events_and_rollouts_and_forbidden():
    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    src = _k8s({
        "/events": {"items": [
            {"metadata": {}, "lastTimestamp": recent, "type": "Warning", "reason": "BackOff", "count": 4,
             "involvedObject": {"kind": "Pod", "name": "orders-1"}, "message": "Back-off restarting"},
            {"metadata": {}, "lastTimestamp": old, "type": "Normal", "reason": "Pulled", "involvedObject": {}}]},
        "/deployments": {"items": [{"metadata": {"name": "orders", "generation": 5},
                                    "spec": {"replicas": 2, "template": {"spec": {"containers": [{"image": "orders:v9"}]}}},
                                    "status": {"readyReplicas": 1, "updatedReplicas": 2, "observedGeneration": 5,
                                               "conditions": [{"type": "Progressing", "reason": "ReplicaSetUpdated"}]}}]},
    })
    assert [e["reason"] for e in src.query("events 15")] == ["BackOff"]
    roll = src.query("rollouts")[0]
    assert (roll["wanted"], roll["ready"], roll["image"]) == (2, 1, "orders:v9")
    src.client = _client(lambda r: httpx.Response(403, text="forbidden"))
    with pytest.raises(SourceError, match="forbidden"):
        src.query("pods")


def test_new_types_are_registered_and_container_checks_accept_kubernetes(tmp_path):
    srcs = build_sources({"k": {"type": "kubernetes", "url": "http://api"}, "l": {"type": "loki", "url": "http://loki"}})
    assert isinstance(srcs["k"], KubernetesSource) and isinstance(srcs["l"], LokiSource)
    from overwatch.config import load_config
    path = tmp_path / "ow.yaml"
    path.write_text("system: {name: s, description: d}\nsources:\n  k: {type: kubernetes, url: 'http://api'}\n"
                    "checks:\n  - {name: api_up, kind: container, service: api}\nllm: {base_url: http://x, api_key: k, investigator_model: m}\n")
    assert load_config(path).checks[0].source == "k"


def test_docker_reports_health_only_for_running_containers():
    from overwatch.sources.docker import DockerSource

    class FakeApi:
        def containers(self, project):
            return [{"Id": "a", "Labels": {"com.docker.compose.service": "db"}, "Names": ["/db"], "Image": "pg"},
                    {"Id": "b", "Labels": {"com.docker.compose.service": "web"}, "Names": ["/web"], "Image": "web"}]

        def inspect(self, cid):
            status = "exited" if cid == "a" else "running"
            return {"State": {"Status": status, "ExitCode": 0, "OOMKilled": False, "StartedAt": "x", "FinishedAt": "0001",
                              "Health": {"Status": "unhealthy" if cid == "a" else "healthy"}},
                    "HostConfig": {}, "RestartCount": 0}

    src = DockerSource.__new__(DockerSource)
    src.api, src.project, src.service_label, src.max_rows = FakeApi(), None, "com.docker.compose.service", 50
    rows = {r["service"]: r for r in src.query("containers")}
    assert rows["db"]["health"] is None  # a stopped container's stale 'unhealthy' means nothing
    assert rows["web"]["health"] == "healthy"


def test_prometheus_nan_and_infinity_become_null():
    from overwatch.sources.prometheus import _num

    assert _num("NaN") is None and _num("+Inf") is None and _num("-Inf") is None and _num("2.5") == 2.5


def test_durations_are_plain_text_never_pythons_minus_one_day():
    from datetime import timedelta

    from overwatch.sources.base import duration_text
    from overwatch.sources.sql import _jsonable

    assert duration_text(timedelta(seconds=-0.003)) == "-0.00 s"  # was "-1 day, 23:59:59.997": read as a day
    assert duration_text(timedelta(seconds=2.5)) == "2.50 s"
    assert duration_text(timedelta(minutes=5)) == "5.0 min"
    assert duration_text(timedelta(hours=3)) == "3.0 h"
    assert _jsonable(timedelta(seconds=-0.003)) == "-0.00 s"
