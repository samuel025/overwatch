"""Load the demo's past incidents (opsdb.tickets) into H-MEM, as if Overwatch had handled them before.

  python seed_memory.py [--memory-url http://127.0.0.1:8099]
"""

from __future__ import annotations

import argparse
import os

import httpx
import psycopg

PG = os.environ.get("DEMO_PG", "postgresql://postgres:postgres@localhost:55432")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--memory-url", default=os.environ.get("MEMORY_URL", "http://127.0.0.1:8099"))
    args = p.parse_args()

    with psycopg.connect(f"{PG}/opsdb") as conn:
        tickets = conn.execute("SELECT key, created::date, title, resolution FROM tickets ORDER BY created").fetchall()

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
