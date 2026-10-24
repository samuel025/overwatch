"""Replay the critic over stored incidents, without touching any live system.

For each stored incident that has a conclusion, run only the critic (one fast-model call) and print which statements
it says the data does not show. Use it to check the critic against diagnoses you know were right or wrong.

  python eval/replay_critic.py [--config examples/demo-system/overwatch.yaml] [--limit 30]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from overwatch.agents.investigator import Investigator  # noqa: E402
from overwatch.config import load_config, load_dotenv  # noqa: E402
from overwatch.llm import LLM  # noqa: E402
from overwatch.store import IncidentStore  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(ROOT / "examples" / "demo-system" / "overwatch.yaml"))
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--db", action="append", help="a SQLite incident store (repeatable); default: the configured state database")
    args = p.parse_args()
    load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    cfg.sources = {}
    stores = [(path, IncidentStore(path)) for path in (args.db or [])] or [
        ("state", IncidentStore(cfg.state_url or os.environ.get("OVERWATCH_STATE_URL") or "traces/overwatch.db"))]
    inv = Investigator(cfg, LLM(cfg.llm, ROOT / "traces"), {})
    for label, store in stores:
        for inc in store.load(limit=args.limit):
            if not inc.root_cause or inc.status == "failed" or not inc.evidence:
                continue
            claims = inv.critique(inc, [])
            print(f"[{label}] {inc.id} conf {inc.confidence:.2f} | {inc.root_cause[:110]!r}")
            print("   critic:", " | ".join(c[:90] for c in claims) if claims else "nothing flagged", flush=True)


if __name__ == "__main__":
    main()
