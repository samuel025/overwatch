"""Check that each fault really shows up in the data, without involving any model.

For every fault: reset, wait for a clean baseline, inject, wait, then record which Overwatch
checks fire plus a snapshot of metrics and error logs. Writes a markdown report.

  python verify_faults.py [fault ...] [--out ../../eval/fault-verification.md]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import psycopg

from faults import FAULTS, PG, reset, wait_until_healthy

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from overwatch.agents import Watcher  # noqa: E402
from overwatch.config import load_config  # noqa: E402
from overwatch.sources import build_sources  # noqa: E402

CONFIG = Path(__file__).resolve().parent.parent / "overwatch.yaml"
BASELINE_SECONDS = 50


def snapshot() -> tuple[list[tuple], list[tuple]]:
    try:
        with psycopg.connect(f"{PG}/opsdb", connect_timeout=3) as conn:
            metrics = conn.execute(
                "SELECT service, round(avg(rps)::numeric, 1), round(avg(p95_ms)::numeric), "
                "round(avg(error_rate)::numeric, 3) FROM request_metrics "
                "WHERE ts > now() - interval '40 seconds' GROUP BY 1 ORDER BY 1"
            ).fetchall()
            logs = conn.execute(
                "SELECT service, left(message, 70), count(*) FROM app_logs WHERE ts > now() - interval '90 seconds' "
                "GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 4"
            ).fetchall()
        return metrics, logs
    except psycopg.OperationalError as e:
        return [("database unreachable", str(e).splitlines()[0][:60], "", "")], []


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("faults", nargs="*", default=list(FAULTS))
    p.add_argument("--out", default=str(Path(__file__).resolve().parents[3] / "eval" / "fault-verification.md"))
    args = p.parse_args()

    cfg = load_config(CONFIG)
    lines = ["# Fault verification", "", "Each fault injected into the demo shop; which Watcher checks fired and what the data showed.", ""]
    for name in args.faults:
        f = FAULTS[name]
        print(f"== {name}: resetting", flush=True)
        reset()
        watcher = Watcher(cfg.checks, build_sources(cfg.sources))
        time.sleep(BASELINE_SECONDS)  # let the 40s metric windows roll past the reset
        wait_until_healthy(watcher)
        before = [a.check for a in watcher.run()]
        f.apply()
        print(f"   injected, waiting {f.settle_seconds}s", flush=True)
        time.sleep(f.settle_seconds)
        fired = watcher.run()
        metrics, logs = snapshot()
        if before:
            verdict = "INVALID: system was already unhealthy before injection"
        elif f.trigger == "watcher":
            verdict = "OK" if fired else "NOT DETECTED"
        else:
            verdict = "OK (surfaces via report)"
        print(f"   fired: {[a.check for a in fired]} {verdict}", flush=True)

        lines += [f"## {name}: {f.title} ({verdict})", "",
                  f"- surfaces via: **{f.trigger}**" + (f" (\"{f.report}\")" if f.report else ""),
                  f"- baseline checks firing before injection: {before or 'none'}",
                  "- checks fired after injection: " + (", ".join(a.summary() for a in fired) or "none"), "",
                  "| service | rps | p95 ms | error rate |", "|---|---|---|---|",
                  *[f"| {' | '.join(str(x) for x in m)} |" for m in metrics], ""]
        if logs:
            lines += ["Top log lines (last 90s):", "", *[f"- {s}: `{m}` x{c}" for s, m, c in logs], ""]
        Path(args.out).write_text("\n".join(lines) + "\n")

    reset()
    print(f"report: {args.out}")


if __name__ == "__main__":
    main()
