"""Checks run by run.sh: discovery and queries against a live cluster and a live Loki."""
import json
import sys
import time
import urllib.request

from overwatch.discovery import discover
from overwatch.sources.kubernetes import KubernetesSource
from overwatch.sources.loki import LokiSource

token, api, loki = sys.argv[1:4]


def check(ok: bool, what: str) -> None:
    print(("PASS  " if ok else "FAIL  ") + what)
    if not ok:
        sys.exit(1)


# --- Kubernetes -------------------------------------------------------------------------------------------------------
plan = discover(docker=None, kubernetes={"url": api, "token": token, "namespace": "shop", "verify": False})
check(not plan["errors"], f"discovery ran without errors {plan['errors']}")
check(set(plan["services"]) == {"api", "web", "worker"}, f"services found from labels: {sorted(plan['services'])}")
check(any(c["name"] == "worker_running" for c in plan["checks"]), "a liveness check was written for each service")
check(plan["sources"]["k8s"].get("verify") is False, "the certificate choice is kept in the generated source")
src = KubernetesSource("k8s", {**plan["sources"]["k8s"]})
pods = src.query("pods")
worker = next(p for p in pods if p.get("service") == "worker" or "worker" in str(p.get("pod", "")))
check(int(worker.get("restarts", 0)) >= 1 or "crash" in json.dumps(worker).lower() or "waiting" in json.dumps(worker).lower(),
      f"the crash-looping worker is visible in `pods`: {json.dumps(worker)[:110]}")
check(any("BackOff" in json.dumps(r) and "worker" in json.dumps(r) for r in src.query("events 60")),
      "`events` shows the worker's BackOff warning (the signal a crash loop leaves)")
check(any("database" in str(r) for r in src.query("errors worker 20")), "the worker's own error line is readable with `errors worker`")

# --- Loki -------------------------------------------------------------------------------------------------------------
now = time.time_ns()
for app, lines in {"api": ["GET /health 200"] * 3 + ["ERROR upstream payments returned 502"] * 2, "web": ["GET / 200"] * 3}.items():
    body = {"streams": [{"stream": {"app": app, "namespace": "shop"},
                         "values": [[str(now - (len(lines) - i) * 5_000_000_000), l] for i, l in enumerate(lines)]}]}
    urllib.request.urlopen(urllib.request.Request(f"{loki}/loki/api/v1/push", data=json.dumps(body).encode(),
                                                  headers={"Content-Type": "application/json"}))
time.sleep(5)
lplan = discover(docker=None, loki={"url": loki})
check(not lplan["errors"] and {"api", "web"} <= set(lplan["services"]), f"Loki discovery found services {sorted(lplan['services'])}")
lsrc = LokiSource("loki", lplan["sources"]["loki"])
api_check = next(c for c in lplan["checks"] if c["name"] == "api_log_errors")
value = lsrc.query(api_check["query"])[0]["value"]
check(value == 2, f"the generated api error check counts the 2 error lines (got {value})")
check(len(lsrc.query('{app="api"} |= "ERROR"')) == 2, "a LogQL log query returns the matching lines")
print("\nAll Kubernetes and Loki checks passed.")
