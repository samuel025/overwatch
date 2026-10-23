"""overwatch CLI.

  overwatch doctor      -c overwatch.yaml   # is everything reachable? (sources, AI provider, memory)
  overwatch check       -c overwatch.yaml   # one Watcher pass, investigate anything wrong
  overwatch investigate -c overwatch.yaml "checkout is slow"
  overwatch run         -c overwatch.yaml   # continuous watching + web UI on :8080
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .config import load_config
from .notifiers import render_text
from .orchestrator import Overwatch


def _ok(label: str, good: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if good else 'FAIL'}] {label}{': ' + detail if detail else ''}")
    return good


def cmd_doctor(ow: Overwatch) -> int:
    print(f"Overwatch doctor for '{ow.cfg.system_name}'")
    good = True
    print("Sources")
    for name, src in ow.sources.items():
        try:
            src.query("vector(1)" if src.language == "PromQL" else "containers" if name == "docker" or
                      src.cfg.get("type") == "docker" else "SELECT 1")
            good &= _ok(name, True, src.language)
        except Exception as e:
            good &= _ok(name, False, str(e))
    print("AI provider")
    try:
        models = ow.llm.health()
        good &= _ok(ow.cfg.llm.base_url, True, f"{len(models)} models/routes available")
        if ow.cfg.llm.investigator_model not in models:
            print(f"  [warn] investigator_model '{ow.cfg.llm.investigator_model}' not in /models list: {', '.join(models[:8])}")
    except Exception as e:
        good &= _ok(ow.cfg.llm.base_url or "(no base_url)", False, str(e))
    print("Memory")
    h = ow.memory.health()
    good &= _ok(h.get("engine", ow.memory.name), bool(h.get("ok")), h.get("error", f"{h.get('memories', '?')} memories"))
    print("Checks")
    for c in ow.cfg.checks:
        if c.kind == "probe":
            up = ow.watcher._probe(c) == 1
            good &= _ok(c.name, up, f"{c.url} {'answered' if up else 'did not answer'}")
            continue
        if c.kind == "container":
            try:
                n = ow.sources[c.source].running(c.service)
                good &= _ok(c.name, n > 0, f"{n} running container{'s' if n != 1 else ''} for {c.service}")
            except Exception as e:
                good &= _ok(c.name, False, str(e))
            continue
        try:
            rows = ow.sources[c.source].query(c.query)
            _ok(c.name, True, f"returned {rows[:1]}")
        except Exception as e:
            good &= _ok(c.name, False, str(e))
    if ow.cfg.impacts:
        import time as _time

        from .impact import measure

        print("Impact queries (last 10 minutes)")
        for item in measure(ow.sources, ow.cfg.impacts, _time.time() - 600):
            good &= _ok(item["label"], "error" not in item, item.get("error") or f"{item['value']} {item['unit']}".strip())
    if ow.cfg.topology:
        print("Topology: " + "; ".join(f"{s} -> {', '.join(d)}" for s, d in ow.cfg.topology.items()))
    print("Notifiers: " + (", ".join(n.name for n in ow.responder.notifiers) or "none"))
    return 0 if good else 1


def _print_progress(inc) -> None:
    ev = inc.evidence[-1]
    status = f"ERROR {ev.error}" if ev.error else f"{len(ev.rows)} rows"
    print(f"  query {len(inc.evidence)} [{ev.source}] {ev.note}: {status}")


def _finish(ow: Overwatch, inc, assume_yes: bool) -> int:
    print(render_text(inc))
    if inc.status != "awaiting_approval":
        return 0
    answer = "y" if assume_yes else input("\nApprove and send to notifiers? [y/N/d=dismiss] ").strip().lower()
    if answer == "y":
        for name, receipt in ow.approve(inc.id).items():
            print(f"  {name}: {receipt}")
    elif answer == "d":
        ow.dismiss(inc.id)
        print("  dismissed")
    return 0


def confirmed_pass(ow: Overwatch) -> list:
    """Run the Watcher until a breach is confirmed or clearly absent (confirm_samples passes)."""
    import time as _time

    anomalies = ow.watcher.run()
    for _ in range(ow.cfg.confirm_samples - 1):
        if anomalies or not ow.watcher.suspected:
            break
        _time.sleep(min(ow.cfg.interval_seconds, 5))
        anomalies = ow.watcher.run()
    return anomalies


def cmd_check(ow: Overwatch, assume_yes: bool) -> int:
    anomalies = confirmed_pass(ow)
    for c in ow.cfg.checks:
        print(f"  {c.name:<32} {ow.watcher.last_values.get(c.name)!s:>12}  ({c.comparison} {c.threshold})")
    if not anomalies:
        print("All checks healthy.")
        return 0
    print(f"\n{len(anomalies)} anomaly(ies) detected. Investigating...")
    inc = ow.handle(anomalies, on_update=_print_progress)
    return _finish(ow, inc, assume_yes)


def cmd_investigate(ow: Overwatch, symptom: str, service: str, assume_yes: bool) -> int:
    print(f"Investigating: {symptom}")
    inc = ow.investigate_symptom(symptom, service, on_update=_print_progress)
    return _finish(ow, inc, assume_yes)


def cmd_run(ow: Overwatch | None, host: str, port: int, insecure: bool, config_path: str) -> int:
    """Serve the console. With no config yet, it opens on the setup page."""
    import os

    import uvicorn

    from .server import create_app

    token = ow.cfg.console_token if ow else os.environ.get("OVERWATCH_TOKEN", "")
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not token and not insecure:
        print(f"Refusing to serve the console on {host} without a token: anyone on the network could approve "
              "reports (or, in setup mode, write configuration). Set OVERWATCH_TOKEN, or pass --insecure.")
        return 2
    url = f"http://{host}:{port}" + (f"/?token={token}" if token else "")
    if ow:
        print(f"Overwatch watching '{ow.cfg.system_name}' every {ow.cfg.interval_seconds}s. Console: {url}")
    else:
        print(f"No {config_path} yet. Open {url} to connect your system (or run `overwatch connect`).")
    uvicorn.run(create_app(ow, config_path=config_path), host=host, port=port, log_level="warning")
    return 0


def cmd_connect(args) -> int:
    """Discover a system and write overwatch.yaml + .env."""
    from .discovery import discover, render, write

    databases = {}
    for spec in args.database or []:
        name, _, url = spec.partition("=")
        if not url:
            print(f"--database expects NAME=URL, got '{spec}'")
            return 2
        databases[name] = url
    docker = {"project": args.project or "", "service_label": args.service_label or ""} if args.docker else None
    if not (docker or args.prometheus or databases):
        print("Nothing to discover: pass --docker, --prometheus URL and/or --database NAME=URL")
        return 2
    plan = discover(docker, args.prometheus or "", databases)
    for e in plan["errors"]:
        print(f"  [!] {e}")
    if plan["errors"] and not plan["checks"]:
        return 1
    print(f"Found {len(plan['services'])} services: {', '.join(sorted(plan['services'])) or '-'}")
    if plan["topology"]:
        print("Topology: " + "; ".join(f"{s} -> {', '.join(d) or '-'}" for s, d in plan["topology"].items()))
    print(f"Proposed {len(plan['checks'])} checks:")
    for c in plan["checks"]:
        print(f"  - {c['name']:<32} {c.get('why', '')}")
    print(f"Evidence pack: {len(plan['evidence'])} queries" + (f"; sources: {', '.join(plan['sources'])}" if plan["sources"] else ""))
    if plan["externals"]:
        print("External services found (added to the topology; remove any you do not want):")
        for e in plan["externals"]:
            print(f"  - {e['name']:<28} called by {', '.join(e['callers']) or '?'} ({'; '.join(e['evidence'])})")
    for n in plan["notes"]:
        print(f"  note: {n}")
    if args.print:
        print(render(plan)[0])
        return 0
    result = write(plan, args.config)
    print(f"\nWrote {result['config']}" + (f" (previous version kept as {result['backup']})" if result["backup"] else ""))
    if result["secrets"]:
        print(f"Secrets ({', '.join(result['secrets'])}) are in {result['env']}")
    print(f"Next: fill in the TODOs, then `overwatch -c {args.config} doctor` and `overwatch -c {args.config} run`")
    return 0


def cmd_change(args) -> int:
    """Report a release/config change to a running Overwatch (for CI pipelines)."""
    import os

    import httpx

    token = os.environ.get("OVERWATCH_TOKEN", "")
    body = {"service": args.service, "version": args.version or "", "kind": args.kind,
            "author": args.author or os.environ.get("USER", ""), "description": args.description or ""}
    try:
        r = httpx.post(f"{args.url.rstrip('/')}/api/changes", json=body, timeout=10,
                       headers={"Authorization": f"Bearer {token}"} if token else {})
    except httpx.HTTPError as e:
        print(f"Could not reach Overwatch at {args.url}: {e}")
        return 1
    if r.status_code != 200:
        print(f"Overwatch answered {r.status_code}: {r.text[:200]}")
        return 1
    print(f"Recorded {args.kind} of {args.service} {args.version or ''}".rstrip())
    return 0


def cmd_init(path: str) -> int:
    target = Path(path)
    if target.exists():
        print(f"{target} already exists; not overwriting")
        return 1
    template = Path(__file__).parent / "templates" / "overwatch.example.yaml"
    shutil.copy(template, target)
    print(f"Wrote {target}. Edit sources, catalog and checks for your system, then run `overwatch doctor -c {target}`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="overwatch", description="Multi-agent incident investigator")
    p.add_argument("-c", "--config", default="overwatch.yaml")
    p.add_argument("--memory", choices=["hmem", "local", "none"], help="override the memory backend")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="check connectivity to sources, AI provider and memory")
    c = sub.add_parser("check", help="run the checks once and investigate anomalies")
    c.add_argument("-y", "--yes", action="store_true", help="approve without asking")
    i = sub.add_parser("investigate", help="investigate a described problem")
    i.add_argument("symptom")
    i.add_argument("--service", default="unknown")
    i.add_argument("-y", "--yes", action="store_true")
    r = sub.add_parser("run", help="watch continuously and serve the web UI")
    r.add_argument("--host", default="127.0.0.1")
    r.add_argument("--port", type=int, default=8080)
    r.add_argument("--insecure", action="store_true", help="allow a non-local host without a console token")
    n = sub.add_parser("init", help="write a starter overwatch.yaml")
    n.add_argument("path", nargs="?", default="overwatch.yaml")
    k = sub.add_parser("connect", help="discover your system and write overwatch.yaml")
    k.add_argument("--docker", action="store_true", help="discover services from Docker / Docker Compose")
    k.add_argument("--project", help="Docker Compose project (if several are running)")
    k.add_argument("--service-label", help="container label naming the service (default: compose service)")
    k.add_argument("--prometheus", metavar="URL", help="Prometheus base URL")
    k.add_argument("--database", metavar="NAME=URL", action="append", help="a SQL database (repeatable)")
    k.add_argument("--print", action="store_true", help="print the config instead of writing it")
    ch = sub.add_parser("change", help="report a release or config change to a running Overwatch")
    ch.add_argument("--service", required=True)
    ch.add_argument("--version")
    ch.add_argument("--kind", default="deploy", choices=["deploy", "config", "rollback", "infra"])
    ch.add_argument("--description")
    ch.add_argument("--author")
    ch.add_argument("--url", default="http://127.0.0.1:8080")
    args = p.parse_args(argv)

    if args.cmd == "init":
        return cmd_init(args.path)
    if args.cmd == "connect":
        return cmd_connect(args)
    if args.cmd == "change":
        from .config import load_dotenv

        load_dotenv()
        return cmd_change(args)
    if args.cmd == "run" and not Path(args.config).exists():
        from .config import load_dotenv

        load_dotenv()
        load_dotenv(Path(args.config).resolve().parent / ".env")
        return cmd_run(None, args.host, args.port, args.insecure, args.config)

    cfg = load_config(args.config)
    override = None
    if args.memory:
        override = {**cfg.memory, "type": args.memory} if args.memory == cfg.memory.get("type") else {"type": args.memory}
    ow = Overwatch(cfg, memory_override=override)
    if args.cmd == "doctor":
        return cmd_doctor(ow)
    if args.cmd == "check":
        return cmd_check(ow, args.yes)
    if args.cmd == "investigate":
        return cmd_investigate(ow, args.symptom, args.service, args.yes)
    if args.cmd == "run":
        return cmd_run(ow, args.host, args.port, args.insecure, args.config)
    return 2


if __name__ == "__main__":
    sys.exit(main())
