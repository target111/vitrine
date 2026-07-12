"""Supervised background workers tied to the bot lifecycle.

Two shapes, one decorator::

    @bot.worker(every=30)                     # periodic job
    async def reconcile(order_service, delivery):
        for order in await order_service.stale():
            await delivery.send(order.chat_id, order_screen(order))

    @bot.worker()                             # long-running supervised loop
    async def chain_watcher(feed, delivery):
        async for block in feed.stream():     # crash -> logged, restarted with backoff
            ...

Workers get dependency injection like handlers (providers, ``bot``,
``delivery`` — but no ``update``/``context``: there is none). They start after
the bot is initialized and are cancelled gracefully on shutdown. Crashes are
logged and the worker is restarted with exponential backoff; app authors never
hand-roll task supervision.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from .injection import Invocation, Providers, resolve_kwargs
from .logging import log_event

logger = logging.getLogger("vitrine.workers")

#: a run longer than this is considered healthy and resets the backoff
_HEALTHY_RUNTIME = 30.0


@dataclass(frozen=True)
class WorkerSpec:
    fn: Callable[..., Awaitable[Any]]
    name: str
    every: float | None = None  # None -> long-running loop
    initial_delay: float = 0.0
    backoff_base: float = 1.0
    backoff_max: float = 60.0


class WorkerSupervisor:
    def __init__(
        self, providers: Providers, make_invocation: Callable[[str], Invocation]
    ) -> None:
        self._providers = providers
        self._make_invocation = make_invocation
        self._specs: list[WorkerSpec] = []
        self._tasks: list[asyncio.Task[None]] = []

    def add(self, spec: WorkerSpec) -> None:
        self._specs.append(spec)

    def start(self) -> None:
        for spec in self._specs:
            task = asyncio.create_task(
                self._supervise(spec), name=f"vitrine-worker-{spec.name}"
            )
            self._tasks.append(task)

        if self._specs:
            logger.info(
                "started %d worker(s): %s",
                len(self._specs),
                ", ".join(s.name for s in self._specs),
            )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_once(self, spec: WorkerSpec) -> None:
        inv = self._make_invocation(f"worker:{spec.name}")
        try:
            kwargs = await resolve_kwargs(spec.fn, inv, self._providers)
            await spec.fn(**kwargs)
        finally:
            await inv.aclose()

    async def _supervise(self, spec: WorkerSpec) -> None:
        if spec.initial_delay:
            await asyncio.sleep(spec.initial_delay)

        failures = 0
        while True:
            started = time.monotonic()
            try:
                await self._run_once(spec)
            except asyncio.CancelledError:
                logger.debug("worker %s cancelled", spec.name)
                raise
            except Exception:
                failures += 1
                delay = min(spec.backoff_max, spec.backoff_base * (2 ** (failures - 1)))
                logger.exception(
                    "worker %s crashed (failure #%d); restarting in %.1fs",
                    spec.name,
                    failures,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            ran_for = time.monotonic() - started
            if ran_for >= _HEALTHY_RUNTIME:
                failures = 0

            if spec.every is not None:
                log_event(
                    logger,
                    "worker.run",
                    level=logging.DEBUG,
                    worker=spec.name,
                    ms=round(ran_for * 1000),
                )
                await asyncio.sleep(spec.every)
            else:
                # a long-running loop returned: treat like a soft failure and restart
                failures += 1
                delay = min(spec.backoff_max, spec.backoff_base * (2 ** (failures - 1)))
                logger.warning(
                    "worker %s exited unexpectedly; restarting in %.1fs",
                    spec.name,
                    delay,
                )
                await asyncio.sleep(delay)
