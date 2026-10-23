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
            src.query("SELECT 1" if src.language == "SQL" else "vector(1)")
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


def cmd_check(ow: Overwatch, assume_yes: bool) -> int:
    anomalies = ow.watcher.run()
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


def cmd_run(ow: Overwatch, host: str, port: int, insecure: bool) -> int:
    import uvicorn

    from .server import create_app

    token = ow.cfg.console_token
    local = host in ("127.0.0.1", "localhost", "::1")
    if not local and not token and not insecure:
        print(f"Refusing to serve the console on {host} without a token: anyone on the network could approve "
              "reports. Set OVERWATCH_TOKEN (or console.token in the config), or pass --insecure.")
        return 2
    url = f"http://{host}:{port}" + (f"/?token={token}" if token else "")
    print(f"Overwatch watching '{ow.cfg.system_name}' every {ow.cfg.interval_seconds}s. Console: {url}")
    uvicorn.run(create_app(ow), host=host, port=port, log_level="warning")
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
    args = p.parse_args(argv)

    if args.cmd == "init":
        return cmd_init(args.path)

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
        return cmd_run(ow, args.host, args.port, args.insecure)
    return 2


if __name__ == "__main__":
    sys.exit(main())
