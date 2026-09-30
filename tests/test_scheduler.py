"""Persistent scheduler: lifecycle transitions, retries, recovery, concurrency, tenant credentials."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import List

import httpx
import pytest

from agents.publisher import PublisherAgent
from app.scheduler import SchedulerService, publish_offloop, scheduler_mode
from app.services import resolve_task_credentials
from db.sqlite_store import SQLiteStore
from models.schemas import ContentDraft, Platform, PublishStatus, PublishTask

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
PAGE = "1000000000001"


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(str(tmp_path / "sched.db"))


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", PAGE)
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "EAAB_default_page_token")


def _service(store, publisher=None, **kw) -> SchedulerService:
    svc = SchedulerService(store, publisher or PublisherAgent(), now_fn=lambda: NOW, **kw)
    svc.retry_base_seconds = 60.0
    return svc


def _seed(store, tenant="default", at=NOW - timedelta(seconds=30), task_id="t1", platform=Platform.META, draft=True) -> PublishTask:
    d = ContentDraft(id=f"draft-{task_id}", tenant_id=tenant, campaign_id="camp", platform=platform, language="en", body="Hello", hashtags=["#x"])
    if draft:
        store.save_draft(d)
    return store.save_task(PublishTask(
        id=task_id, tenant_id=tenant, campaign_id="camp", content_draft_id=d.id, platform=platform,
        status=PublishStatus.SCHEDULED, scheduled_at=at,
    ))


def _run(coro):
    return asyncio.run(coro)


def test_scheduler_mode_parsing(monkeypatch):
    monkeypatch.setenv("SCHEDULER_MODE", "worker")
    assert scheduler_mode() == "worker"
    monkeypatch.setenv("SCHEDULER_MODE", "nonsense")
    assert scheduler_mode() == "embedded"
    monkeypatch.setenv("SCHEDULER_MODE", "OFF")
    assert scheduler_mode() == "off"


# --------------------------------------------------------------- happy path
def test_due_task_goes_scheduled_publishing_published_with_full_log(store, graph_stub, creds):
    graph_stub.add("POST", f"/{PAGE}/photos", json={"id": "1", "post_id": f"{PAGE}_77"})
    _seed(store)
    assert _run(_service(store).poll_once()) == 1

    task = store.get_task("t1")
    assert task.status == PublishStatus.PUBLISHED
    assert task.external_post_id == f"{PAGE}_77" and task.post_url == f"https://www.facebook.com/{PAGE}_77"
    assert task.attempts == 1 and task.published_at is not None
    messages = [log.message for log in task.logs]
    assert messages[0] == "Claimed by scheduler" and "Publishing started" in messages and "Publish succeeded" in messages


def test_only_due_scheduled_tasks_are_touched(store, graph_stub, creds):
    graph_stub.add("POST", "/photos", json={"id": "1", "post_id": f"{PAGE}_1"})
    _seed(store, task_id="due")
    _seed(store, task_id="future", at=NOW + timedelta(hours=2))
    _seed(store, task_id="never", at=None)
    assert _run(_service(store).poll_once()) == 1
    assert store.get_task("future").status == PublishStatus.SCHEDULED and store.get_task("never").status == PublishStatus.SCHEDULED
    assert len(graph_stub.calls) == 1


def test_scheduled_posts_survive_a_restart(tmp_path, graph_stub, creds):
    """The DB is the job store: a brand-new process (new store + service objects) still runs the task."""
    graph_stub.add("POST", "/photos", json={"id": "1", "post_id": f"{PAGE}_9"})
    path = str(tmp_path / "restart.db")
    _seed(SQLiteStore(path), at=NOW + timedelta(minutes=5))

    later = NOW + timedelta(minutes=6)
    restarted = SchedulerService(SQLiteStore(path), PublisherAgent(), now_fn=lambda: later)
    assert _run(restarted.poll_once()) == 1
    assert SQLiteStore(path).get_task("t1").status == PublishStatus.PUBLISHED


# ------------------------------------------------------------------ failures
def test_permanent_failure_is_final_and_logged_in_detail(store, graph_stub, creds):
    graph_stub.add("POST", "/photos", status=400, json={"error": {"message": "Error validating access token", "code": 190, "fbtrace_id": "TRACE1"}})
    _seed(store)
    _run(_service(store).poll_once())
    task = store.get_task("t1")
    assert task.status == PublishStatus.FAILED and "expired" in task.error.lower()
    assert task.attempts == 1 and len(graph_stub.calls) == 1  # not retried
    assert any(log.level == "ERROR" and log.data.get("graph_error", {}).get("fbtrace_id") == "TRACE1" for log in task.logs)
    assert store.list_due_tasks(NOW + timedelta(days=1)) == []  # nothing left to run


def test_transient_network_failure_is_retried_with_backoff_then_gives_up(store, creds, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("network down")

    monkeypatch.setattr("tools.meta_api.get_http_client", lambda: httpx.Client(transport=httpx.MockTransport(boom)))
    _seed(store)
    clock = {"now": NOW}
    svc = SchedulerService(store, PublisherAgent(), now_fn=lambda: clock["now"])
    svc.retry_base_seconds, svc.max_attempts = 60.0, 3

    _run(svc.poll_once())
    t = store.get_task("t1")
    assert t.status == PublishStatus.SCHEDULED and t.attempts == 1
    assert t.scheduled_at == NOW + timedelta(seconds=60)
    assert any("will retry" in log.message for log in t.logs)

    assert _run(svc.poll_once()) == 0  # backoff not elapsed yet
    clock["now"] = NOW + timedelta(seconds=61)
    _run(svc.poll_once())
    t = store.get_task("t1")
    assert t.status == PublishStatus.SCHEDULED and t.attempts == 2
    assert t.scheduled_at == NOW + timedelta(seconds=61) + timedelta(seconds=120)  # doubled

    clock["now"] = NOW + timedelta(hours=1)
    _run(svc.poll_once())
    t = store.get_task("t1")
    assert t.status == PublishStatus.FAILED and t.attempts == 3
    assert t.error and "Connection exception" in t.error


def test_missed_schedule_window_fails_instead_of_publishing_stale_post(store, graph_stub, creds):
    """Protects against bursts of old posts when the scheduler comes up on a stale database."""
    _seed(store, at=NOW - timedelta(days=5))
    _run(_service(store).poll_once())
    t = store.get_task("t1")
    assert t.status == PublishStatus.FAILED and "Missed schedule window" in t.error
    assert graph_stub.calls == []


def test_missing_draft_fails_cleanly(store, graph_stub, creds):
    _seed(store, draft=False)
    _run(_service(store).poll_once())
    t = store.get_task("t1")
    assert t.status == PublishStatus.FAILED and "not found" in t.error
    assert graph_stub.calls == []


def test_unexpected_crash_never_leaves_task_stuck_in_publishing(store, creds):
    _seed(store)

    def exploding_resolver(task):
        raise RuntimeError("resolver blew up")

    _run(_service(store, credentials_resolver=exploding_resolver).poll_once())
    t = store.get_task("t1")
    assert t.status == PublishStatus.FAILED and "resolver blew up" in t.error


# -------------------------------------------------------------- crash recovery
def test_stale_publishing_tasks_are_recovered_and_rerun(store, graph_stub, creds):
    graph_stub.add("POST", "/photos", json={"id": "1", "post_id": f"{PAGE}_3"})
    _seed(store)
    assert store.claim_task("t1", expected_status="scheduled", new_status="publishing")  # worker "crashes" here
    # The store stamps updated_at with the real clock, so the service clock must be relative to it.
    svc = SchedulerService(store, PublisherAgent(), now_fn=lambda: datetime.now(timezone.utc) + timedelta(minutes=30))
    svc.stale_after = timedelta(minutes=5)
    svc.max_late = timedelta(days=3650)
    assert svc.recover_stuck_tasks() == 1
    assert store.get_task("t1").status == PublishStatus.SCHEDULED
    _run(svc.poll_once())
    t = store.get_task("t1")
    assert t.status == PublishStatus.PUBLISHED and t.attempts == 2
    assert any("Recovered task stuck" in log.message for log in t.logs)


def test_recent_publishing_tasks_are_left_alone(store):
    _seed(store)
    store.claim_task("t1", expected_status="scheduled", new_status="publishing")
    svc = _service(store)
    svc.stale_after = timedelta(hours=1)
    assert svc.recover_stuck_tasks() == 0
    assert store.get_task("t1").status == PublishStatus.PUBLISHING


def test_recovery_gives_up_after_max_attempts(store):
    _seed(store)
    for _ in range(3):
        store.claim_task("t1", expected_status="scheduled", new_status="publishing")
        store.claim_task("t1", expected_status="publishing", new_status="scheduled")
    store.claim_task("t1", expected_status="scheduled", new_status="publishing")
    svc = SchedulerService(store, PublisherAgent(), now_fn=lambda: datetime.now(timezone.utc) + timedelta(hours=1))
    svc.max_attempts, svc.stale_after = 3, timedelta(minutes=1)
    assert svc.recover_stuck_tasks() == 1
    t = store.get_task("t1")
    assert t.status == PublishStatus.FAILED and "gave up" in t.error


# ----------------------------------------------------------------- concurrency
def test_two_workers_never_publish_the_same_task_twice(store, graph_stub, creds):
    graph_stub.add("POST", "/photos", json={"id": "1", "post_id": f"{PAGE}_1"})
    calls: List[str] = []

    class CountingPublisher(PublisherAgent):
        async def publish(self, task, draft, **kw):
            calls.append(task.id)
            await asyncio.sleep(0.05)
            return await super().publish(task, draft, **kw)

    for i in range(6):
        _seed(store, task_id=f"t{i}")

    async def race():
        a, b = _service(store, CountingPublisher()), _service(store, CountingPublisher())
        return await asyncio.gather(a.poll_once(), b.poll_once(), a.poll_once(), b.poll_once())

    executed = _run(race())
    assert sorted(calls) == sorted(set(calls)) == [f"t{i}" for i in range(6)]
    assert sum(executed) == 6
    assert all(store.get_task(f"t{i}").status == PublishStatus.PUBLISHED for i in range(6))


def test_leader_lock_denial_skips_polling(store):
    class Denied:
        def acquire_or_renew(self):
            return False

        def release(self):
            pass

    _seed(store)
    assert _run(_service(store, leader_lock=Denied()).poll_once()) == 0
    assert store.get_task("t1").status == PublishStatus.SCHEDULED


def test_run_forever_polls_and_stops(store, graph_stub, creds):
    graph_stub.add("POST", "/photos", json={"id": "1", "post_id": f"{PAGE}_5"})
    _seed(store)

    async def scenario():
        svc = _service(store)
        svc.poll_seconds = 1.0
        task = asyncio.create_task(svc.run_forever())
        for _ in range(50):
            await asyncio.sleep(0.05)
            if store.get_task("t1").status == PublishStatus.PUBLISHED:
                break
        svc.stop()
        await asyncio.wait_for(task, timeout=5)

    _run(scenario())
    assert store.get_task("t1").status == PublishStatus.PUBLISHED


# ------------------------------------------------- multi-tenant credentials
def test_tenant_task_publishes_with_the_tenants_own_page_token(store, graph_stub, creds):
    graph_stub.add("POST", "/PAGE_ACME/photos", json={"id": "1", "post_id": "PAGE_ACME_1"})
    tenant = store.create_tenant("Acme")["id"]
    store.save_connected_account("u", "meta", "PAGE_ACME", "Acme Page", "ACME_PAGE_TOKEN_123", tenant_id=tenant)
    _seed(store, tenant=tenant)
    _run(_service(store, credentials_resolver=lambda t: resolve_task_credentials(store, t)).poll_once())

    t = store.get_task("t1", tenant_id=tenant)
    assert t.status == PublishStatus.PUBLISHED and t.external_post_id == "PAGE_ACME_1"
    url = str(graph_stub.calls[0].url)
    assert "/PAGE_ACME/photos" in url and "ACME_PAGE_TOKEN_123" in url
    assert "EAAB_default_page_token" not in url and PAGE not in url


def test_tenant_without_page_never_uses_the_default_workspace_page(store, graph_stub, creds):
    tenant = store.create_tenant("NoPage")["id"]
    _seed(store, tenant=tenant)
    _run(_service(store, credentials_resolver=lambda t: resolve_task_credentials(store, t)).poll_once())
    assert graph_stub.calls == []  # the default workspace's Facebook page was never touched
    t = store.get_task("t1", tenant_id=tenant)
    assert t.confirmation_badge == "Credentials Missing 🟡"
    assert any(log.data.get("mode") == "simulated" for log in t.logs)


def test_default_tenant_uses_process_credentials(store, graph_stub, creds):
    graph_stub.add("POST", f"/{PAGE}/photos", json={"id": "1", "post_id": f"{PAGE}_1"})
    _seed(store)
    _run(_service(store, credentials_resolver=lambda t: resolve_task_credentials(store, t)).poll_once())
    assert "EAAB_default_page_token" in str(graph_stub.calls[0].url)


def test_publish_offloop_runs_publisher_on_a_thread(creds):
    draft = ContentDraft(campaign_id="c", platform=Platform.TIKTOK, language="en", body="b")
    task = PublishTask(campaign_id="c", content_draft_id=draft.id, platform=Platform.TIKTOK, status=PublishStatus.PUBLISHING)
    out = _run(publish_offloop(PublisherAgent(), task, draft))
    assert out.status == PublishStatus.PUBLISHED
