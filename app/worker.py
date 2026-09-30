"""Standalone scheduler worker: ``python -m app.worker``.

Run exactly one (or several; publishing is CAS-protected) of these next to the web
containers when ``SCHEDULER_MODE=worker``.  It shares the database with the web app,
so scheduled posts survive restarts of either process.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from agents.publisher import PublisherAgent
from app.config import load_environment
from app.scheduler import SchedulerService, build_leader_lock
from app.services import resolve_task_credentials
from db import create_store

logger = logging.getLogger("omniflow.worker")


async def _run() -> None:
    load_environment()
    store = create_store()
    publisher = PublisherAgent()
    service = SchedulerService(
        store,
        publisher,
        credentials_resolver=lambda task: resolve_task_credentials(store, task),
    )
    service.leader_lock = build_leader_lock(service.poll_seconds)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, service.stop)
        except NotImplementedError:  # Windows event loops
            signal.signal(sig, lambda *_: service.stop())

    await service.run_forever()
    store.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(_run())


if __name__ == "__main__":
    main()
