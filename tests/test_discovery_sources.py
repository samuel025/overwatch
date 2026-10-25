"""Kubernetes and Loki discovery, and the config they render, against recorded-style API responses."""

import httpx
import yaml

from overwatch.config import load_config
from overwatch.discovery import discover, render
from overwatch.discovery.kubernetes import discover_kubernetes
from overwatch.discovery.loki import discover_loki


def _pod(name, svc, image, env=None, phase="Running"):
    return {"metadata": {"name": name, "labels": {"app": svc}},
            "spec": {"containers": [{"image": image, "env": env or [],
                                     "resources": {"limits": {"cpu": "500m", "memory": "256Mi"}},
                                     "readinessProbe": {"httpGet": {"path": "/healthz", "port": 8000}}}]},
            "status": {"phase": phase, "containerStatuses": [{"ready": True, "restartCount": 0}]}}


def k8s_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/pods"):
        return httpx.Response(200, json={"items": [
            _pod("gateway-1", "gateway", "gw:1", [{"name": "ORDERS_URL", "value": "http://orders.shop.svc:8000"},
                                                  {"name": "SECRET", "valueFrom": {"secretKeyRef": {"name": "s"}}}]),
            _pod("orders-1", "orders", "orders:1", [{"name": "PSP_URL", "value": "https://api.stripe.com/v1"}]),
            _pod("orders-2", "orders", "orders:1"),
            _pod("db-0", "db", "postgres:16")]})
    if path.endswith("/log"):
        return httpx.Response(200, text="2026-10-23T10:00:00Z ERROR POST https://api.stripe.com/v1/charge timeout\n",
                              headers={"content-type": "text/plain"})
    return httpx.Response(404)


def test_kubernetes_discovery_finds_services_dependencies_and_externals():
    d = discover_kubernetes(url="http://api", namespace="shop", transport=httpx.MockTransport(k8s_handler))
    assert set(d["services"]) == {"gateway", "orders", "db"}
    assert d["topology"] == {"gateway": ["orders"]}  # from ORDERS_URL naming the orders service
    assert d["services"]["db"]["what"] == "PostgreSQL database server"
    assert "2 pods in namespace shop" in d["services"]["orders"]["runs_on"] and "readiness probe /healthz" in d["services"]["orders"]["runs_on"]
    assert {c["name"] for c in d["checks"]} == {"gateway_running", "orders_running", "db_running"}
    assert all(c["kind"] == "container" and c["source"] == "k8s" for c in d["checks"])
    assert d["env_hits"]["orders"] == {"api.stripe.com": "PSP_URL"}  # hostname only, never the value
    assert d["log_hits"]["orders"] == {"api.stripe.com": 2}
    assert "OOMKilled" in d["runtime_facts"] and "shop" in d["internal"]


def loki_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/loki/api/v1/labels":
        return httpx.Response(200, json={"data": ["filename", "app", "level"]})
    if path == "/loki/api/v1/label/app/values":
        return httpx.Response(200, json={"data": ["payments", "orders"]})
    return httpx.Response(404)


def test_loki_discovery_builds_error_checks_per_service():
    d = discover_loki("http://loki", transport=httpx.MockTransport(loki_handler))
    assert d["label"] == "app" and d["services"] == ["orders", "payments"]
    check = next(c for c in d["checks"] if c["name"] == "payments_log_errors")
    assert check["query"].startswith('sum(count_over_time({app="payments"}') and check["relative_factor"] == 4
    assert d["evidence"][0]["query"].startswith("sum by (app)")


def test_loki_without_a_service_label_says_what_it_found():
    def handler(request):
        return httpx.Response(200, json={"data": ["filename", "level"]})
    try:
        discover_loki("http://loki", transport=httpx.MockTransport(handler))
    except RuntimeError as e:
        assert "filename, level" in str(e)
    else:
        raise AssertionError("expected a RuntimeError")


def test_full_plan_renders_a_config_that_loads(tmp_path, monkeypatch):
    from overwatch.discovery import kubernetes as kmod, loki as lmod

    real_k, real_l = kmod.discover_kubernetes, lmod.discover_loki
    monkeypatch.setattr("overwatch.discovery.discover_kubernetes",
                        lambda *a, **k: real_k(*a, **k, transport=httpx.MockTransport(k8s_handler)))
    monkeypatch.setattr("overwatch.discovery.discover_loki",
                        lambda *a, **k: real_l(*a, **k, transport=httpx.MockTransport(loki_handler)))
    plan = discover(None, "", None, kubernetes={"url": "http://api", "token": "tok-123", "namespace": "shop"},
                    loki={"url": "http://loki", "tenant": "t1"})
    assert plan["errors"] == [] and plan["log_source"] == {"type": "kubernetes", "source": "k8s"}
    ext = next(e for e in plan["externals"] if e["name"] == "api.stripe.com")
    ext.update(callers=["orders"], log_text="stripe", health_url="")
    text, env = render(plan)
    assert env["OW_K8S_TOKEN"] == "tok-123" and "tok-123" not in text  # the token never lands in the YAML
    doc = yaml.safe_load(text)
    assert doc["sources"]["k8s"]["token"] == "${OW_K8S_TOKEN}" and doc["sources"]["loki"]["tenant"] == "t1"
    assert any(c["name"] == "api_stripe_com_errors" and c["query"] == "count orders 2 stripe" and c["source"] == "k8s"
               for c in doc["checks"])
    monkeypatch.setenv("OW_K8S_TOKEN", "tok-123")
    path = tmp_path / "overwatch.yaml"
    path.write_text(text)
    cfg = load_config(path)
    assert {c.name for c in cfg.checks} >= {"orders_running", "payments_log_errors"}
    assert cfg.topology["gateway"] == ["orders"]


def test_a_cluster_with_its_own_certificate_authority_can_be_connected_and_the_choice_is_saved():
    # no verify given: the system CAs are used, and nothing extra is written to the config
    d = discover_kubernetes(url="https://api", namespace="shop", transport=httpx.MockTransport(k8s_handler))
    assert "verify" not in d["source"]
    # a CA file or "skip": both reach the source block, which is what the running Overwatch reads
    import certifi  # a real CA bundle to point at: the HTTP client loads the file it is given
    ca = discover_kubernetes(url="https://api", namespace="shop", transport=httpx.MockTransport(k8s_handler), verify=certifi.where())
    assert ca["source"]["verify"] == certifi.where()
    skip = discover_kubernetes(url="https://api", namespace="shop", transport=httpx.MockTransport(k8s_handler), verify=False)
    assert skip["source"]["verify"] is False
    plan = discover(docker=None, kubernetes={"url": "https://api", "namespace": "shop", "verify": False})  # via the wizard path
    assert plan["errors"] and "kubernetes request failed" in plan["errors"][0]  # unreachable here, but it got that far


def test_a_certificate_error_tells_the_user_how_to_fix_it():
    def refuse(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate")
    try:
        discover_kubernetes(url="https://api", namespace="shop", transport=httpx.MockTransport(refuse))
    except RuntimeError as e:
        assert "CA file" in str(e) and "certificate verification" in str(e)
    else:
        raise AssertionError("expected an error")


def test_the_setup_form_turns_ca_and_skip_into_one_verify_setting():
    from overwatch.server import KubernetesIn
    assert KubernetesIn().verify() is True
    assert KubernetesIn(ca_file=" /etc/ca.crt ").verify() == "/etc/ca.crt"
    assert KubernetesIn(ca_file="/etc/ca.crt", skip_tls=True).verify() is False  # skipping wins
