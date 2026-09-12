import pytest
from unittest.mock import patch
from datetime import datetime, timezone
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def prevent_env_file_mutation():
    with patch("app.main._update_env_file"):
        yield


def test_health():

    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert "service" in data


def test_dashboard_and_cors():
    res = client.get("/")
    assert res.status_code == 200
    assert "OmniFlow AI" in res.text

    res_dash = client.get("/dashboard")
    assert res_dash.status_code == 200
    assert "Campaign Analytics" in res_dash.text

    # Test CORS header
    res_cors = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert res_cors.status_code == 200
    assert res_cors.headers.get("access-control-allow-origin") in ["*", "http://localhost:3000"]


def test_campaign_create_standard():
    payload = {
        "name": "Summer Launch",
        "objective": "Brand awareness and conversions",
        "budget": 500,
        "currency": "USD",
        "platforms": ["meta", "tiktok", "xiaohongshu"],
        "schedule": {
            "start_at": datetime.now(timezone.utc).isoformat(),
            "frequency_per_day": 2,
            "posting_times": ["10:00", "18:00"],
        },
        "languages": ["en", "zh"],
        "brand_voice": "energetic, modern",
        "product_description": "Exclusive summer gifts",
    }
    res = client.post("/campaign/create", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "Summer Launch"
    assert "strategy" in data
    assert "budget_allocation" in data["strategy"]


def test_campaign_create_swagger_defaults():
    # Simulate Swagger UI default "Try it out" payload with "string" values
    payload = {
        "name": "Swagger Test Campaign",
        "objective": "Testing schema resilience",
        "target_audience": {
            "age_range": "string",
            "genders": ["string"],
            "locations": ["string"],
            "interests": ["string"],
            "languages": ["string"],
            "persona": "string",
        },
        "budget": 0,
        "currency": "USD",
        "platforms": ["meta"],
        "schedule": {
            "start_at": "2026-09-12T07:18:29.832Z",
            "end_at": "2026-09-12T07:18:29.832Z",
            "timezone": "UTC",
            "posting_times": ["string"],
            "frequency_per_day": 1,
        },
        "languages": ["string"],
        "brand_voice": "clear, helpful, modern",
        "product_description": "string",
    }
    res = client.post("/campaign/create", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "planned"


def test_campaign_create_empty_payload():
    res = client.post("/campaign/create", json={})
    assert res.status_code == 200
    data = res.json()
    assert data["name"] == "Global Growth Campaign"


def test_list_campaigns():
    res = client.get("/campaigns")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) >= 1


def test_content_generate_and_swagger_fallbacks():
    # 1. Generate with Swagger default payload (campaign_id="string", media_links=["string"])
    swagger_payload = {
        "campaign_id": "string",
        "topic": "string",
        "platforms": ["meta", "instagram"],
        "count_per_platform": 1,
        "media_links": ["string"],
    }
    res = client.post("/content/generate", json=swagger_payload)
    assert res.status_code == 200
    drafts = res.json()
    assert len(drafts) >= 1
    assert all("id" in d and "body" in d for d in drafts)


def test_publish_schedule_and_mock_fallback():
    # 1. Publish immediate with mock fallback for unknown draft IDs
    res = client.post("/publish/schedule", json={
        "content_draft_ids": ["string"],
        "publish_now": True,
    })
    assert res.status_code == 200
    tasks = res.json()
    assert len(tasks) == 1
    assert tasks[0]["status"] == "published"
    assert tasks[0]["external_post_id"] is not None

    # 2. Schedule for future
    future_iso = "2026-09-20T12:00:00Z"
    res_sched = client.post("/publish/schedule", json={
        "content_draft_ids": ["string"],
        "publish_now": False,
        "scheduled_at": future_iso,
    })
    assert res_sched.status_code == 200
    sched_tasks = res_sched.json()
    assert len(sched_tasks) == 1
    assert sched_tasks[0]["status"] == "scheduled"


def test_analytics_report_with_metrics():
    res = client.get("/analytics/report?period_days=30")
    assert res.status_code == 200
    report = res.json()
    assert "channels" in report
    assert "totals" in report
    totals = report["totals"]
    assert "impressions" in totals
    assert "clicks" in totals
    assert "roi_percentage" in totals
    assert "ctr_percentage" in totals
    assert len(report["recommendations"]) >= 1


def test_demo_seed_endpoint():
    from unittest.mock import patch
    with patch("agents.copywriter.CopywriterAgent._call_openai", return_value={"body": "Seeded body", "cta": "Explore", "hashtags": ["#summer"]}):
        res = client.post("/demo/seed")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "seeded"
        assert data["drafts_count"] >= 1
        assert data["tasks_count"] >= 1


def test_auth_flow():
    # 1. Register with idempotent unique email
    from uuid import uuid4
    unique_email = f"lead_{uuid4().hex[:8]}@omniflow.ai"
    reg_payload = {
        "email": unique_email,
        "password": "SecurePassword99!",
        "full_name": "Jordan Bell",
        "company": "Growth Ventures",
        "role": "Marketing Director"
    }
    res_reg = client.post("/auth/register", json=reg_payload)
    assert res_reg.status_code == 200
    reg_data = res_reg.json()
    assert reg_data["user"]["full_name"] == "Jordan Bell"
    assert "token" in reg_data

    # 2. Login
    login_payload = {
        "email": unique_email,
        "password": "SecurePassword99!"
    }
    res_login = client.post("/auth/login", json=login_payload)
    assert res_login.status_code == 200
    login_data = res_login.json()
    assert login_data["user"]["email"] == unique_email

    # 3. /auth/me
    res_me = client.get("/auth/me")
    assert res_me.status_code == 200
    assert "email" in res_me.json()


def test_integrations_flow():
    # 1. Query status
    res_status = client.get("/integrations/status")
    assert res_status.status_code == 200
    data = res_status.json()
    assert "meta" in data
    assert "instagram" in data
    assert "tiktok" in data
    assert "x" in data

    # 2. Connect Meta / Instagram
    res_conn = client.post("/integrations/connect", json={
        "platform": "meta",
        "account_id": "act-live-page-99",
        "account_name": "Test Brand Facebook Page",
        "access_token": "EAAB_test_token_live"
    })
    assert res_conn.status_code == 200
    conn_data = res_conn.json()
    assert conn_data["status"] == "connected"

    # 3. OAuth URL
    res_oauth = client.get("/auth/oauth/meta/url")
    assert res_oauth.status_code == 200
    assert "oauth_url" in res_oauth.json()


def test_facebook_graph_api_publishing_and_resilience():
    from unittest.mock import patch, MagicMock

    # 1. Test successful Facebook publish via Graph API mock
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b'{"id": "101728504668130_9988776655"}'
    mock_resp.json.return_value = {"id": "101728504668130_9988776655"}

    with patch("requests.post", return_value=mock_resp):
        res = client.post("/publish/schedule", json={
            "content_draft_ids": ["test-meta-draft"],
            "publish_now": True,
            "channel": "meta"
        })
        assert res.status_code == 200
        tasks = res.json()
        assert len(tasks) == 1
        t = tasks[0]
        assert t["status"] == "published"
        assert t["external_post_id"] == "101728504668130_9988776655"
        assert t["post_url"] == "https://www.facebook.com/101728504668130_9988776655"
        assert "Published to" in t["confirmation_badge"]

    # 2. Test live resilience (using current environment credentials with graceful error handling)
    res_live = client.post("/publish/schedule", json={
        "content_draft_ids": ["test-meta-draft-live"],
        "publish_now": True,
        "channel": "meta"
    })
    assert res_live.status_code == 200
    live_tasks = res_live.json()
    assert len(live_tasks) == 1
    lt = live_tasks[0]
    assert lt["status"] == "published"
    assert lt["confirmation_badge"] is not None


def test_facebook_tools_status():
    res = client.get("/tools/facebook/status")
    assert res.status_code == 200
    data = res.json()
    assert "connected" in data
    assert "page_id" in data


def test_assistant_chat_faq_and_directives():
    # 1. Test FAQ question in English
    res_faq = client.post("/assistant/chat", json={
        "message": "How do I publish a post to Facebook?"
    })
    assert res_faq.status_code == 200
    data_faq = res_faq.json()
    assert data_faq["type"] in ("chat", "content_generation")
    assert "Facebook" in data_faq["reply"]

    # 2. Test FAQ question in English
    res_en = client.post("/assistant/chat", json={
        "message": "What can you do?"
    })
    assert res_en.status_code == 200
    data_en = res_en.json()
    assert data_en["type"] == "chat"
    assert "OmniFlow" in data_en["reply"]

    # 3. Test Boost directive
    res_boost = client.post("/assistant/chat", json={
        "message": "/boost"
    })
    assert res_boost.status_code == 200
    assert res_boost.json()["type"] == "boost"

    # 4. Test Content generation directive
    from unittest.mock import patch
    with patch("agents.copywriter.CopywriterAgent._call_openai", return_value={"body": "Organic espresso beans", "cta": "Buy Now", "hashtags": ["#coffee"]}):
        res_gen = client.post("/assistant/chat", json={
            "message": "Create ad for organic espresso beans"
        })
        assert res_gen.status_code == 200
        assert res_gen.json()["type"] == "content_generation"
        assert len(res_gen.json()["drafts"]) > 0


def test_facebook_token_update_validation():
    # Empty token rejected
    res = client.post("/tools/facebook/update-token", json={"access_token": ""})
    assert res.status_code == 400
    
    # Fake token rejected with 400 or 500
    res_fake = client.post("/tools/facebook/update-token", json={"access_token": "invalid_fake_token_123"})
    assert res_fake.status_code in (400, 500)
    
    # Mock valid token verification
    from unittest.mock import patch, MagicMock
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "101728504668130", "name": "Mai boovoo"}
    mock_resp.content = b'{"id": "101728504668130", "name": "Mai boovoo"}'
    
    with patch("requests.get", return_value=mock_resp), patch("app.main._update_env_file"):
        res_ok = client.post("/tools/facebook/update-token", json={
            "access_token": "EAAB_mock_valid_access_token",
            "page_id": "101728504668130"
        })
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data["success"] is True
        assert data["page_name"] == "Mai boovoo"
        assert "Connected to Mai boovoo" in data["confirmation_badge"]


def test_publish_meta_photo_pipeline():
    from unittest.mock import patch, MagicMock
    # Tiny valid 1x1 transparent PNG base64
    tiny_png_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    
    mock_photo_res = {
        "success": True,
        "id": "101728504668130_1234567890",
        "photo_id": "1234567890",
        "post_url": "https://www.facebook.com/101728504668130_1234567890",
        "confirmation_badge": "Published to Mai boovoo 🟢",
        "status_code": 200,
        "message": "Published to Mai boovoo 🟢",
    }
    
    with patch("tools.meta_api.MetaAPIClient.publish_facebook_page_photo", return_value=mock_photo_res) as mock_photo:
        res = client.post("/publish/schedule", json={
            "content_draft_ids": ["test-photo-draft"],
            "publish_now": True,
            "channel": "meta",
            "copy_text": "Live photo showcase post",
            "image_base64": tiny_png_b64
        })
        assert res.status_code == 200
        tasks = res.json()
        assert len(tasks) == 1
        assert tasks[0]["status"] == "published"
        assert tasks[0]["post_url"] == "https://www.facebook.com/101728504668130_1234567890"
        assert "Published to Mai boovoo" in tasks[0]["confirmation_badge"]
        mock_photo.assert_called_once()


def test_publish_facebook_endpoint_no_token():
    # If FACEBOOK_PAGE_ACCESS_TOKEN is not in environment, /publish/facebook returns 400
    with patch.dict("os.environ", {}, clear=True):
        res = client.post("/publish/facebook", json={"caption": "Test post"})
        assert res.status_code == 400
        assert "FACEBOOK_PAGE_ACCESS_TOKEN not configured" in res.json()["detail"]


def test_publish_facebook_endpoint_photo_success():
    from unittest.mock import patch, MagicMock
    tiny_png_b64 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "101728504668130_998877"}
    mock_resp.text = '{"id": "101728504668130_998877"}'

    with patch.dict("os.environ", {"FACEBOOK_PAGE_ACCESS_TOKEN": "valid_meta_test_token"}), \
         patch("requests.post", return_value=mock_resp) as mock_post:
        res = client.post("/publish/facebook", json={
            "caption": "Photo creative caption",
            "image_base64": tiny_png_b64,
            "page_id": "101728504668130"
        })
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["post_id"] == "101728504668130_998877"
        assert data["post_url"] == "https://facebook.com/101728504668130_998877"
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "photos" in call_url


def test_publish_facebook_endpoint_feed_success():
    from unittest.mock import patch, MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "101728504668130_554433"}
    mock_resp.text = '{"id": "101728504668130_554433"}'

    with patch.dict("os.environ", {"FACEBOOK_PAGE_ACCESS_TOKEN": "valid_meta_test_token"}), \
         patch("requests.post", return_value=mock_resp) as mock_post:
        res = client.post("/publish/facebook", json={
            "caption": "Text only creative caption",
            "page_id": "101728504668130"
        })
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["post_id"] == "101728504668130_554433"
        assert data["post_url"] == "https://facebook.com/101728504668130_554433"
        mock_post.assert_called_once()
        call_url = mock_post.call_args[0][0]
        assert "feed" in call_url


def test_publish_facebook_endpoint_api_error():
    from unittest.mock import patch, MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = '{"error": {"message": "Invalid OAuth access token"}}'

    with patch.dict("os.environ", {"FACEBOOK_PAGE_ACCESS_TOKEN": "invalid_meta_test_token"}), \
         patch("requests.post", return_value=mock_resp):
        res = client.post("/publish/facebook", json={
            "caption": "Failing test post",
            "page_id": "101728504668130"
        })
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is False
        assert "Invalid OAuth access token" in data["error"]
        assert data["status_code"] == 400


def test_publish_facebook_endpoint_empty_env_page_id():
    from unittest.mock import patch, MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "101728504668130_888"}
    mock_resp.text = '{"id": "101728504668130_888"}'

    with patch.dict("os.environ", {"FACEBOOK_PAGE_ACCESS_TOKEN": "valid_token", "FACEBOOK_PAGE_ID": ""}), \
         patch("requests.post", return_value=mock_resp) as mock_post:
        res = client.post("/publish/facebook", json={
            "caption": "Empty env page id test",
            "page_id": "101728504668130"
        })
        assert res.status_code == 200
        assert res.json()["success"] is True
        call_url = mock_post.call_args[0][0]
        assert "101728504668130" in call_url


def test_publish_facebook_endpoint_malformed_base64():
    from unittest.mock import patch, MagicMock

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "101728504668130_777"}
    mock_resp.text = '{"id": "101728504668130_777"}'

    # When base64 is malformed, it should gracefully fall back to feed endpoint without 500 crash
    with patch.dict("os.environ", {"FACEBOOK_PAGE_ACCESS_TOKEN": "valid_token"}), \
         patch("requests.post", return_value=mock_resp) as mock_post:
        res = client.post("/publish/facebook", json={
            "caption": "Malformed base64 fallback test",
            "image_base64": "not_valid_base64_data_string_that_is_long_enough_to_trigger_photo_branch_1234567890",
            "page_id": "101728504668130"
        })
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["post_id"] == "101728504668130_777"
        call_url = mock_post.call_args[0][0]
        assert "feed" in call_url


def test_publish_facebook_endpoint_raw_base64():
    from unittest.mock import patch, MagicMock
    raw_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"id": "101728504668130_666"}
    mock_resp.text = '{"id": "101728504668130_666"}'

    with patch.dict("os.environ", {"FACEBOOK_PAGE_ACCESS_TOKEN": "valid_token"}), \
         patch("requests.post", return_value=mock_resp) as mock_post:
        res = client.post("/publish/facebook", json={
            "caption": "Raw base64 test without data prefix",
            "image_base64": raw_b64,
            "page_id": "101728504668130"
        })
        assert res.status_code == 200
        assert res.json()["success"] is True
        call_url = mock_post.call_args[0][0]
        assert "photos" in call_url






