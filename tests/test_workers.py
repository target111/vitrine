"""Worker supervision: crash backoff and its reset behaviour."""

from __future__ import annotations

import asyncio

from vitrine.injection import Invocation, Providers
from vitrine.workers import WorkerSpec, WorkerSupervisor


async def test_periodic_success_resets_backoff(monkeypatch):
    """A completed periodic run is healthy: the next crash backs off from scratch."""
    real_sleep = asyncio.sleep
    delays: list[float] = []

    async def fake_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    script = ["fail", "fail", "ok", "fail"]

    async def flaky():
        if script and script.pop(0) == "fail":
            raise RuntimeError("boom")

    supervisor = WorkerSupervisor(
        Providers(), lambda name: Invocation(handler_name=name)
    )
    task = asyncio.create_task(
        supervisor._supervise(WorkerSpec(fn=flaky, name="flaky", every=0.0))
    )
    async with asyncio.timeout(5):
        while len(delays) < 4:
            await real_sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # crash (1s), crash (2s), healthy run (sleep `every`), crash: back to 1s, not 4s
    assert delays[:4] == [1.0, 2.0, 0.0, 1.0]
