"""Benchmark Overwatch on the demo fault catalog, with H-MEM memory on vs off.

For each fault: reset the shop, wait for a clean baseline, inject the fault, then run a full
investigation twice on the same broken system: once with H-MEM, once with no memory.
A diagnosis is correct when its root cause (plus recommended actions) mentions at least one
keyword from every `must_mention` group of the fault's ground truth.

Needs: demo system up, memory service running and seeded, Nightshift running.

  python eval/run_eval.py                      # all faults
  python eval/run_eval.py slow_query lock_contention --modes hmem
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo-system"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEMO / "scripts"))

from faults import FAULTS, reset, wait_until_healthy  # noqa: E402

from overwatch.agents import Watcher  # noqa: E402
from overwatch.config import load_config  # noqa: E402
from overwatch.orchestrator import Overwatch  # noqa: E402
from overwatch.sources import build_sources  # noqa: E402

BASELINE_SECONDS = 50


def grade(fault, text: str) -> tuple[bool, list[str]]:
    low = text.lower()
    missing = [" / ".join(group) for group in fault.must_mention if not any(k.lower() in low for k in group)]
    return not missing, missing


def run_once(cfg, fault, mode: str, trace_dir: Path) -> dict:
    cfg.trace_dir = str(trace_dir / mode)
    ow = Overwatch(cfg, memory_override={"type": "none"} if mode == "none" else None)
    started = time.monotonic()
    if fault.trigger == "report":
        incident = ow.investigate_symptom(fault.report)
    else:
        anomalies = ow.watcher.run()
        if not anomalies:
            return {"fault": fault.name, "mode": mode, "detected": False, "correct": False, "seconds": 0, "queries": 0,
                    "root_cause": "", "missing": ["(no check fired)"], "memory_hits": []}
        incident = ow.handle(anomalies)
    text = incident.root_cause + " " + " ".join(incident.recommended_actions)
    correct, missing = grade(fault, text)
    return {
        "fault": fault.name,
        "mode": mode,
        "detected": True,
        "correct": correct,
        "missing": missing,
        "seconds": round(time.monotonic() - started, 1),
        "queries": len(incident.evidence),
        "query_errors": sum(1 for e in incident.evidence if e.error),
        "confidence": incident.confidence,
        "root_cause": incident.root_cause,
        "memory_hits": [m["id"] for m in incident.similar_incidents if m.get("used")],
        "incident": incident.id,
    }


def write_report(results: list[dict], out: Path, model: str) -> None:
    modes = sorted({r["mode"] for r in results})
    lines = [
        "# Overwatch evaluation: H-MEM memory on vs off",
        "",
        f"Run {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}, model `{model}` via Meterless Nightshift, "
        f"{len({r['fault'] for r in results})} faults from `examples/demo-system/scripts/faults.py`.",
        "",
        "## Summary",
        "",
        "| memory | correct root cause | avg seconds | avg queries |",
        "|---|---|---|---|",
    ]
    for m in modes:
        rs = [r for r in results if r["mode"] == m]
        n = max(1, len(rs))
        lines.append(f"| {m} | {sum(r['correct'] for r in rs)}/{len(rs)} | {sum(r['seconds'] for r in rs) / n:.1f} | "
                     f"{sum(r['queries'] for r in rs) / n:.1f} |")
    for label, has_ticket in (("with a similar past incident", True), ("never seen before", False)):
        names = {f.name for f in FAULTS.values() if bool(f.past_ticket) == has_ticket}
        cells = []
        for m in modes:
            rs = [r for r in results if r["mode"] == m and r["fault"] in names]
            if rs:
                cells.append(f"{m}: {sum(r['correct'] for r in rs)}/{len(rs)} correct, "
                             f"{sum(r['seconds'] for r in rs) / len(rs):.1f}s avg")
        if cells:
            lines.append(f"\nFaults {label}: " + "; ".join(cells))
    lines += ["", "## Per fault", "", "| fault | memory | correct | seconds | queries | memory used | root cause |",
              "|---|---|---|---|---|---|---|"]
    for r in results:
        rc = r["root_cause"].replace("|", "/").replace("\n", " ")[:160]
        mark = "yes" if r["correct"] else ("not detected" if not r["detected"] else "no (missing: " + "; ".join(r["missing"]) + ")")
        lines.append(f"| {r['fault']} | {r['mode']} | {mark} | {r['seconds']} | {r['queries']} | "
                     f"{', '.join(r['memory_hits']) or '-'} | {rc} |")
    out.write_text("\n".join(lines) + "\n")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("faults", nargs="*", default=list(FAULTS))
    p.add_argument("--modes", nargs="+", default=["hmem", "none"], choices=["hmem", "none"])
    p.add_argument("--out", default=str(ROOT / "eval" / "results.md"))
    args = p.parse_args()

    cfg = load_config(DEMO / "overwatch.yaml")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    trace_dir = ROOT / "eval" / "traces" / stamp
    results: list[dict] = []
    for name in args.faults:
        fault = FAULTS[name]
        print(f"== {name}", flush=True)
        reset()
        time.sleep(BASELINE_SECONDS)
        if not wait_until_healthy(Watcher(cfg.checks, build_sources(cfg.sources))):
            print("   skipped: system did not return to healthy after reset", flush=True)
            continue
        fault.apply()
        time.sleep(fault.settle_seconds)
        for mode in args.modes:
            r = run_once(cfg, fault, mode, trace_dir / name)
            results.append(r)
            print(f"   {mode:<5} correct={r['correct']} {r['seconds']}s {r['queries']} queries: {r['root_cause'][:100]}", flush=True)
            with (trace_dir / "results.jsonl").open("a") as f:
                f.write(json.dumps(r) + "\n")
        write_report(results, Path(args.out), cfg.llm.investigator_model)
    reset()
    print(f"report: {args.out}")


if __name__ == "__main__":
    main()
