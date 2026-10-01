"""Persistent, restart-safe publish scheduler.

Design
------
The database is the job store: a scheduled post is a ``scheduled_tasks`` row with
``status='scheduled'`` and ``scheduled_at``.  Nothing lives only in memory, so posts
survive restarts, crashes and redeploys.

Lifecycle (every transition is persisted and logged on the task)::

    scheduled --claim (CAS)--> publishing --> published
                                    |------> failed        (permanent error, or attempts exhausted)
                                    '------> scheduled     (transient network error, retry with backoff)
    publishing (stale, worker crashed) --recover--> scheduled (or failed when attempts exhausted)
    scheduled (overdue beyond SCHEDULER_MAX_LATE_SECONDS)   --> failed  ("missed schedule window")

Safety
------
* ``store.claim_task`` is an atomic compare-and-swap, so any number of workers/processes
  can poll concurrently without ever double-publishing a post.
* An optional Redis leader lock (``REDIS_URL``) keeps a single active poller per cluster
  to avoid needless contention; if Redis is unreachable the poller keeps running and
  relies on the CAS alone.
* Overdue tasks older than the grace window are failed rather than published, so bringing
  the scheduler up on a database with stale schedules can never fire a burst of old posts.

Modes (``SCHEDULER_MODE``): ``embedded`` (poller inside the web process; default for dev),
``worker`` (web only enqueues; run ``python -m app.worker``), ``off``.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from agents.publisher import PublisherAgent
from app.config import env_float, env_int, env_str, load_environment
from app.redaction import describe_exception
from db.base import Store
from models.schemas import PublishLog, PublishStatus, PublishTask

logger = logging.getLogger("omniflow.scheduler")

MODE_EMBEDDED = "embedded"
MODE_WORKER = "worker"
MODE_OFF = "off"


def scheduler_mode() -> str:
    load_environment()
    mode = env_str("SCHEDULER_MODE", default=MODE_EMBEDDED).lower()
    return mode if mode in {MODE_EMBEDDED, MODE_WORKER, MODE_OFF} else MODE_EMBEDDED


async def publish_offloop(publisher: PublisherAgent, task: PublishTask, draft: Any, **kwargs: Any) -> PublishTask:
    """Run ``publisher.publish`` (sync HTTP inside) on a worker thread so the event loop stays responsive."""
    return await asyncio.to_thread(lambda: asyncio.run(publisher.publish(task, draft, **kwargs)))


class RedisLeaderLock:
    """Best-effort cluster-wide poller lock (``SET NX EX`` with token-checked renew/release)."""

    KEY = "omniflow:scheduler:leader"

    def __init__(self, url: str, ttl_seconds: int = 60) -> None:
        import redis  # local import: Redis is optional

        self._redis = redis.Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3, decode_responses=True)
        self._ttl = ttl_seconds
        self._token = secrets.token_hex(8)

    def acquire_or_renew(self) -> bool:
        try:
            if self._redis.set(self.KEY, self._token, nx=True, ex=self._ttl):
                return True
            if self._redis.get(self.KEY) == self._token:
                self._redis.expire(self.KEY, self._ttl)
                return True
            return False
        except Exception as exc:  # noqa: BLE001 - Redis down must not stop publishing
            logger.warning("Redis leader lock unavailable (%s); continuing without it", type(exc).__name__)
            return True

    def release(self) -> None:
        try:
            if self._redis.get(self.KEY) == self._token:
                self._redis.delete(self.KEY)
        except Exception:  # noqa: BLE001
            pass


def build_leader_lock(poll_seconds: float) -> Optional[RedisLeaderLock]:
    url = env_str("REDIS_URL")
    if not url:
        return None
    try:
        return RedisLeaderLock(url, ttl_seconds=max(30, int(poll_seconds * 4)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis not usable (%s); scheduler will rely on database claiming only", type(exc).__name__)
        return None


class SchedulerService:
    """Polls the store for due tasks and publishes them with full lifecycle logging."""

    def __init__(
        self,
        store: Store,
        publisher: PublisherAgent,
        *,
        credentials_resolver: Optional[Callable[[PublishTask], Dict[str, Any]]] = None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        leader_lock: Optional[RedisLeaderLock] = None,
    ) -> None:
        load_environment()
        self.store = store
        self.publisher = publisher
        self.credentials_resolver = credentials_resolver
        self.now_fn = now_fn
        self.leader_lock = leader_lock
        self.poll_seconds = max(1.0, env_float("SCHEDULER_POLL_SECONDS", 15.0))
        self.batch_size = max(1, env_int("SCHEDULER_BATCH_SIZE", 20))
        self.max_attempts = max(1, env_int("SCHEDULER_MAX_ATTEMPTS", 3))
        self.max_late = timedelta(seconds=max(60, env_int("SCHEDULER_MAX_LATE_SECONDS", 3600)))
        self.stale_after = timedelta(seconds=max(60, env_int("SCHEDULER_STALE_PUBLISHING_SECONDS", 300)))
        self.retry_base_seconds = max(1.0, env_float("SCHEDULER_RETRY_BASE_SECONDS", 60.0))
        self._stop = asyncio.Event()

    # ------------------------------------------------------------ helpers
    def _save(self, task: PublishTask) -> PublishTask:
        return self.store.save_task(task)

    @staticmethod
    def _log(task: PublishTask, level: str, message: str, **data: Any) -> None:
        task.logs.append(PublishLog(level=level, message=message, data=data))

    def _fail(self, task: PublishTask, message: str, **data: Any) -> PublishTask:
        task.status = PublishStatus.FAILED
        task.error = message
        task.confirmation_badge = task.confirmation_badge or "Publish Failed 🔴"
        self._log(task, "ERROR", message, **data)
        logger.error("Task %s (tenant=%s) failed: %s", task.id, task.tenant_id, message)
        return self._save(task)

    # ------------------------------------------------------------- recovery
    def recover_stuck_tasks(self) -> int:
        """Tasks left in ``publishing`` by a crashed worker go back to ``scheduled`` (or fail)."""
        recovered = 0
        for task in self.store.list_recoverable_tasks(self.now_fn() - self.stale_after):
            if not self.store.claim_task(task.id, expected_status="publishing", new_status="scheduled"):
                continue  # another worker recovered it
            fresh = self.store.get_task_any_tenant(task.id)
            if fresh is None:
                continue
            if fresh.attempts >= self.max_attempts:
                self._fail(fresh, f"Publishing interrupted; gave up after {fresh.attempts} attempts")
            else:
                self._log(fresh, "WARNING", "Recovered task stuck in 'publishing' (worker restart?)", attempts=fresh.attempts)
                self._save(fresh)
            recovered += 1
        if recovered:
            logger.warning("Recovered %d stuck task(s)", recovered)
        return recovered

    # ------------------------------------------------------------ execution
    async def run_task(self, task_id: str) -> Optional[PublishTask]:
        """Claim and execute one task. Returns the final task, or ``None`` if another worker won the claim."""
        if not self.store.claim_task(task_id, expected_status="scheduled", new_status="publishing"):
            return None
        task = self.store.get_task_any_tenant(task_id)
        if task is None:
            return None
        self._log(task, "INFO", "Claimed by scheduler", attempt=task.attempts)

        try:
            due_at = task.scheduled_at
            if due_at is not None:
                if due_at.tzinfo is None:
                    due_at = due_at.replace(tzinfo=timezone.utc)
                late_by = self.now_fn() - due_at
                if late_by > self.max_late:
                    return self._fail(
                        task,
                        f"Missed schedule window: due {due_at.isoformat()}, "
                        f"{int(late_by.total_seconds())}s late (limit {int(self.max_late.total_seconds())}s)",
                        reason="missed_window",
                    )

            draft = self.store.get_draft_any_tenant(task.content_draft_id)
            if draft is None:
                return self._fail(task, f"Draft {task.content_draft_id} not found", reason="draft_missing")

            creds: Dict[str, Any] = {}
            if self.credentials_resolver is not None:
                creds = self.credentials_resolver(task) or {}
            published = await publish_offloop(
                self.publisher, task, draft,
                page_id=creds.get("page_id"), access_token=creds.get("access_token"),
                isolated=bool(creds.get("isolated")),
            )
            return self._finalize(published)
        except Exception as exc:  # noqa: BLE001 - never leave a task stuck in 'publishing'
            logger.exception("Scheduler crashed while publishing task %s", task_id)
            fresh = self.store.get_task_any_tenant(task_id) or task
            return self._fail(fresh, "Unexpected scheduler error: " + describe_exception(exc), reason="exception")

    def _finalize(self, task: PublishTask) -> PublishTask:
        if task.status == PublishStatus.FAILED and self._is_transient(task) and task.attempts < self.max_attempts:
            delay = min(self.retry_base_seconds * (2 ** max(0, task.attempts - 1)), 3600.0)
            task.status = PublishStatus.SCHEDULED
            task.scheduled_at = self.now_fn() + timedelta(seconds=delay)
            self._log(
                task, "WARNING", "Transient failure; will retry",
                attempt=task.attempts, max_attempts=self.max_attempts, retry_in_seconds=delay, error=task.error,
            )
            logger.warning("Task %s transient failure, retry %d/%d in %.0fs", task.id, task.attempts, self.max_attempts, delay)
        return self._save(task)

    @staticmethod
    def _is_transient(task: PublishTask) -> bool:
        for log in reversed(task.logs):
            mode = (log.data or {}).get("mode")
            if mode:
                return mode == "network_exception"
        return False

    # -------------------------------------------------------------- polling
    async def poll_once(self) -> int:
        """One scheduler tick. Returns the number of tasks this worker executed."""
        if self.leader_lock is not None and not await asyncio.to_thread(self.leader_lock.acquire_or_renew):
            return 0
        await asyncio.to_thread(self.recover_stuck_tasks)
        due = await asyncio.to_thread(self.store.list_due_tasks, self.now_fn(), self.batch_size)
        executed = 0
        for task in due:
            result = await self.run_task(task.id)
            if result is not None:
                executed += 1
        if executed:
            logger.info("Scheduler executed %d task(s)", executed)
        return executed

    async def run_forever(self) -> None:
        logger.info(
            "Scheduler loop started (poll=%.0fs, batch=%d, max_attempts=%d, max_late=%ds)",
            self.poll_seconds, self.batch_size, self.max_attempts, int(self.max_late.total_seconds()),
        )
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:  # noqa: BLE001
                logger.exception("Scheduler tick failed; will retry")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
        if self.leader_lock is not None:
            self.leader_lock.release()
        logger.info("Scheduler loop stopped")

    def stop(self) -> None:
        self._stop.set()
