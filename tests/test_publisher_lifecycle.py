"""Publisher state transitions: scheduled -> publishing -> published | failed."""
from __future__ import annotations

import asyncio

import pytest

from agents.publisher import PublisherAgent
from models.schemas import ContentDraft, Platform, PublishStatus, PublishTask

PAGE = "1000000000001"


def _draft(**kw) -> ContentDraft:
    data = dict(campaign_id="c1", platform=Platform.META, language="en", body="Body", hashtags=["#a"])
    data.update(kw)
    return ContentDraft(**data)


def _task(draft: ContentDraft, **kw) -> PublishTask:
    data = dict(campaign_id=draft.campaign_id, content_draft_id=draft.id, platform=draft.platform, status=PublishStatus.PUBLISHING)
    data.update(kw)
    return PublishTask(**data)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", PAGE)
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "EAAB_token")


def test_create_tasks_scheduled_vs_immediate():
    agent = PublisherAgent()
    d = _draft()
    now_task = agent.create_tasks([d], publish_now=True, scheduled_at=None)[0]
    later_task = agent.create_tasks([d], publish_now=False, scheduled_at=None)[0]
    assert now_task.status == PublishStatus.PUBLISHING and now_task.scheduled_at is None
    assert later_task.status == PublishStatus.SCHEDULED
    assert now_task.tenant_id == d.tenant_id


def test_live_success_populates_post_url_and_external_id(graph_stub, creds):
    graph_stub.add("POST", "/photos", json={"id": "1", "post_id": f"{PAGE}_42"})
    draft = _draft()
    task = _run(PublisherAgent().publish(_task(draft), draft))
    assert task.status == PublishStatus.PUBLISHED
    assert task.external_post_id == f"{PAGE}_42"
    assert task.post_url == f"https://www.facebook.com/{PAGE}_42"
    assert task.published_at is not None and task.error is None
    assert "Published to" in task.confirmation_badge
    assert [log.message for log in task.logs][:1] == ["Publishing started"]
    assert any(log.data.get("mode") == "live_facebook" for log in task.logs)


def test_text_only_post_when_no_image_available(graph_stub, creds, monkeypatch):
    monkeypatch.setattr(PublisherAgent, "load_fallback_asset", staticmethod(lambda: None))
    graph_stub.add("POST", "/feed", json={"id": f"{PAGE}_7"})
    draft = _draft()
    task = _run(PublisherAgent().publish(_task(draft), draft))
    assert task.status == PublishStatus.PUBLISHED and task.post_url.endswith(f"{PAGE}_7")
    assert "/feed" in str(graph_stub.calls[0].url)


def test_graph_error_with_credentials_marks_task_failed(graph_stub, creds):
    graph_stub.add("POST", "/photos", status=400, json={"error": {"message": "Error validating access token", "code": 190, "fbtrace_id": "TR"}})
    draft = _draft()
    task = _run(PublisherAgent().publish(_task(draft), draft))
    assert task.status == PublishStatus.FAILED
    assert task.published_at is None and task.external_post_id is None and task.post_url is None
    assert "expired" in task.error.lower()
    assert task.confirmation_badge == "Token Expired 🟡"
    error_logs = [log for log in task.logs if log.level == "ERROR"]
    assert error_logs and error_logs[0].data["graph_error"]["fbtrace_id"] == "TR"


def test_network_error_marks_task_failed_with_network_mode(creds, monkeypatch):
    import httpx

    def boom(request):
        raise httpx.ConnectError("unreachable")

    monkeypatch.setattr("tools.meta_api.get_http_client", lambda: httpx.Client(transport=httpx.MockTransport(boom)))
    draft = _draft()
    task = _run(PublisherAgent().publish(_task(draft), draft))
    assert task.status == PublishStatus.FAILED
    assert any(log.data.get("mode") == "network_exception" for log in task.logs)


def test_unexpected_exception_marks_task_failed_never_published(creds, monkeypatch):
    agent = PublisherAgent()
    monkeypatch.setattr(agent, "_publish_meta_or_instagram", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")))
    draft = _draft()
    task = _run(agent.publish(_task(draft), draft))
    assert task.status == PublishStatus.FAILED
    assert "kaboom" in task.error
    assert task.logs[-1].level == "ERROR"


def test_no_credentials_is_simulated_and_labelled():
    draft = _draft()
    task = _run(PublisherAgent().publish(_task(draft), draft))
    assert task.status == PublishStatus.PUBLISHED
    assert task.external_post_id.startswith("demo-meta-")
    assert task.post_url is None
    assert task.confirmation_badge == "Credentials Missing 🟡"
    assert any(log.data.get("mode") == "simulated" for log in task.logs)


@pytest.mark.parametrize("platform", [Platform.TIKTOK, Platform.X, Platform.XIAOHONGSHU, Platform.WECHAT])
def test_non_meta_platforms_are_simulated(platform):
    draft = _draft(platform=platform)
    task = _run(PublisherAgent().publish(_task(draft), draft))
    assert task.status == PublishStatus.PUBLISHED
    assert any(log.data.get("mode") == "simulated" for log in task.logs)


def test_explicit_tenant_credentials_override_process_credentials(graph_stub, creds):
    graph_stub.add("POST", "/999/photos", json={"id": "1", "post_id": "999_5"})
    draft = _draft()
    task = _run(PublisherAgent().publish(_task(draft), draft, page_id="999", access_token="TENANT_TOKEN"))
    assert task.status == PublishStatus.PUBLISHED and task.external_post_id == "999_5"
    assert "access_token=TENANT_TOKEN" in str(graph_stub.calls[0].url)


def test_isolated_tenant_never_falls_back_to_process_credentials(graph_stub, creds):
    """A tenant without its own page must not publish to the default workspace's page."""
    draft = _draft()
    task = _run(PublisherAgent().publish(_task(draft), draft, page_id=None, access_token=None, isolated=True))
    assert graph_stub.calls == []
    assert task.status == PublishStatus.PUBLISHED and task.confirmation_badge == "Credentials Missing 🟡"
    assert any(log.data.get("mode") == "simulated" for log in task.logs)
