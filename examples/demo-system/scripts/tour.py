"""Live fault tour: break the shop one fault at a time while you watch the Overwatch console.

For each fault: wait until the shop is healthy, inject it, wait until Overwatch has a diagnosis
(or gives up), pause so you can read it, then reset. Ctrl+C stops the tour and resets the shop.

  python tour.py                          # a curated tour
  python tour.py slow_query cert_expired  # just these
  python tour.py --pause 90               # more reading time per fault
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

from faults import FAULTS, reset, wait_until_healthy

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from overwatch.agents import Watcher  # noqa: E402
from overwatch.config import load_config  # noqa: E402
from overwatch.sources import build_sources  # noqa: E402

CONFIG = Path(__file__).resolve().parent.parent / "overwatch.yaml"
DEFAULT_TOUR = ["slow_query", "service_down", "bad_replica", "provider_rate_limit", "lock_contention", "memory_leak"]


def newest_incident(console: str, headers: dict, since: str) -> dict | None:
    try:
        incidents = httpx.get(f"{console}/api/status", headers=headers, timeout=10).json()["incidents"]
    except (httpx.HTTPError, KeyError):
        return None
    mine = [i for i in incidents if i["opened_at"] >= since]
    return min(mine, key=lambda i: i["opened_at"]) if mine else None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("faults", nargs="*", default=DEFAULT_TOUR)
    p.add_argument("--console", default="http://127.0.0.1:8080")
    p.add_argument("--pause", type=int, default=60, help="seconds to leave each diagnosis on screen")
    p.add_argument("--timeout", type=int, default=420, help="max seconds to wait for a diagnosis")
    args = p.parse_args()

    cfg = load_config(CONFIG)
    headers = {"Authorization": f"Bearer {cfg.console_token}"} if cfg.console_token else {}
    watcher = Watcher(cfg.checks, build_sources(cfg.sources))

    try:
        for n, name in enumerate(args.faults, 1):
            fault = FAULTS[name]
            print(f"\n[{n}/{len(args.faults)}] {fault.title} ({name})", flush=True)
            print("  waiting for the shop to be healthy...", flush=True)
            reset()
            time.sleep(30)
            wait_until_healthy(watcher)
            since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
            fault.apply()
            print(f"  BROKEN: {fault.truth}", flush=True)
            print("  watch the console: the map and banner should change within ~30s", flush=True)

            deadline = time.time() + args.timeout
            inc = None
            while time.time() < deadline:
                inc = newest_incident(args.console, headers, since)
                if inc and inc["status"] in ("awaiting_approval", "failed", "reported"):
                    break
                time.sleep(5)
            if inc and inc["status"] != "investigating":
                print(f"  OVERWATCH ({inc['status']}): {inc['root_cause'][:300]}", flush=True)
            else:
                print("  no diagnosis within the time limit", flush=True)
            print(f"  leaving it on screen for {args.pause}s...", flush=True)
            time.sleep(args.pause)
    except KeyboardInterrupt:
        print("\nstopping the tour", flush=True)
    finally:
        reset()
        print("shop reset. Tour over.", flush=True)


if __name__ == "__main__":
    main()
