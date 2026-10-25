"""Load the demo's past incidents (opsdb.tickets) into H-MEM, as if Overwatch had handled them before.

  python seed_memory.py [--memory-url http://127.0.0.1:8099]
"""

from __future__ import annotations

import argparse
import os
import sys
import time

import httpx
import psycopg

PG = os.environ.get("DEMO_PG", "postgresql://postgres:postgres@localhost:15432")


def wait_for_tickets(timeout: int = 180) -> list[tuple]:
    """The demo database seeds 3M orders on its first start, so right after `docker compose up` it is not ready yet."""
    deadline, shown = time.time() + timeout, False
    while True:
        try:
            with psycopg.connect(f"{PG}/opsdb", connect_timeout=5) as conn:
                return conn.execute("SELECT key, created::date, title, resolution FROM tickets ORDER BY created").fetchall()
        except psycopg.Error as e:
            if time.time() > deadline:
                sys.exit(f"The demo database did not become ready in {timeout} s: {str(e).splitlines()[0]}")
            if not shown:
                print("Waiting for the demo database to finish its first start (it loads 3M orders)...", flush=True)
                shown = True
            time.sleep(3)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--memory-url", default=os.environ.get("MEMORY_URL", "http://127.0.0.1:8099"))
    args = p.parse_args()

    tickets = wait_for_tickets()

    for key, created, title, resolution in tickets:
        r = httpx.post(
            f"{args.memory_url}/remember",
            json={
                "content": f"Incident {created} ({key}): {title}. Root cause and fix: {resolution}",
                "source": f"ticket:{key}",
                "tags": [f"ticket:{key}"],
                "layer": "long_term",
                "confidence": 0.85,
            },
            timeout=10,
        )
        r.raise_for_status()
        print(f"{key} -> {r.json()['memory']['id']}")


if __name__ == "__main__":
    main()
