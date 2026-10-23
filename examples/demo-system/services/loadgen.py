"""Steady checkout traffic against the gateway so metrics always have data."""

import asyncio
import os
import random

import httpx

GATEWAY = os.environ.get("GATEWAY_URL", "http://gateway:8000")
RPS = float(os.environ.get("RPS", "15"))
MAX_IN_FLIGHT = int(os.environ.get("MAX_IN_FLIGHT", "100"))  # clients give up rather than queue forever


async def one(client: httpx.AsyncClient, slots: asyncio.Semaphore) -> None:
    try:
        await client.get(f"{GATEWAY}/checkout", params={"customer_id": random.randint(1, 50000)})
    except httpx.HTTPError:
        pass
    finally:
        slots.release()


async def main() -> None:
    slots = asyncio.Semaphore(MAX_IN_FLIGHT)
    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            if slots.locked():  # too many requests in flight: this one is simply not sent
                await asyncio.sleep(random.expovariate(RPS))
                continue
            await slots.acquire()
            asyncio.create_task(one(client, slots))
            await asyncio.sleep(random.expovariate(RPS))


if __name__ == "__main__":
    asyncio.run(main())
