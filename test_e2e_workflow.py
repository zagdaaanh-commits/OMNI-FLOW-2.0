#!/usr/bin/env python
"""
End-to-End Workflow Verification and Data Persistence Test.

Executes the 5 core marketing workflow steps in sequence:
  Step 1: POST /campaign/create
  Step 2: GET /campaigns & GET /campaign/{campaign_id}
  Step 3: POST /content/generate
  Step 4: POST /publish/schedule
  Step 5: GET /analytics/report
Followed by direct SQLite file persistence verification.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone

if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import requests
from fastapi.testclient import TestClient

from app.main import app
from storage import SQLiteStore

# Check if a live server is running on port 5000; otherwise use TestClient
LIVE_URL = "http://127.0.0.1:5000"
use_live = False

try:
    live_check = requests.get(f"{LIVE_URL}/health", timeout=1.0)
    if live_check.status_code == 200:
        use_live = True
except Exception:
    use_live = False


class WorkflowClient:
    """Unified client wrapper supporting either live HTTP or in-process TestClient."""

    def __init__(self, live: bool = False):
        self.live = live
        self.tc = TestClient(app) if not live else None

    def post(self, path: str, json_data: dict) -> requests.Response:
        if self.live:
            return requests.post(f"{LIVE_URL}{path}", json=json_data)
        return self.tc.post(path, json=json_data)

    def get(self, path: str) -> requests.Response:
        if self.live:
            return requests.get(f"{LIVE_URL}{path}")
        return self.tc.get(path)


def run_e2e_workflow():
    client = WorkflowClient(live=use_live)
    mode_str = f"Live Server ({LIVE_URL})" if use_live else "FastAPI Engine (Direct Persistent Client)"

    print("=" * 70)
    print("  MULTI-AGENT MARKETING SYSTEM: END-TO-END WORKFLOW VERIFICATION")
    print(f"  Execution Mode: {mode_str}")
    print("=" * 70)

    # -------------------------------------------------------------
    # STEP 1: POST /campaign/create
    # -------------------------------------------------------------
    print("\n[STEP 1] Creating new campaign via POST /campaign/create ...")
    create_payload = {
        "name": "Global Cross-Border Autumn Launch",
        "objective": "Accelerate brand awareness and sales conversions across international and Asia markets",
        "target_audience": {
            "age_range": "20-45",
            "locations": ["United States", "United Kingdom", "Japan", "Singapore"],
            "interests": ["fashion", "handcrafted gifts", "lifestyle", "cross-border e-commerce"],
            "languages": ["en", "zh"],
        },
        "budget": 1800.0,
        "currency": "USD",
        "platforms": ["meta", "instagram", "tiktok", "xiaohongshu", "douyin", "wechat", "x"],
        "schedule": {
            "start_at": datetime.now(timezone.utc).isoformat(),
            "timezone": "UTC",
            "posting_times": ["11:30", "19:30"],
            "frequency_per_day": 2,
        },
        "languages": ["en", "zh"],
        "brand_voice": "sophisticated, energetic, authentic, modern",
        "product_description": "Artisanal handcrafted leather accessories and luxury gift sets.",
    }

    res_step1 = client.post("/campaign/create", create_payload)
    print(f"  --> HTTP Status: {res_step1.status_code}")
    assert res_step1.status_code == 200, f"Step 1 failed: {res_step1.text}"

    campaign = res_step1.json()
    campaign_id = campaign.get("id")
    assert campaign_id, "Campaign response must contain a valid unique ID"

    print(f"  [OK] Campaign Created Successfully!")
    print(f"       Unique ID       : {campaign_id}")
    print(f"       Campaign Name   : {campaign.get('name')}")
    print(f"       Status          : {campaign.get('status')}")
    print(f"       Allocated Budget: ${campaign.get('budget')} {campaign.get('currency')}")
    print(f"       Target Channels : {campaign.get('platforms')}")
    strategy = campaign.get("strategy", {})
    print(f"       Budget Split    : {strategy.get('budget_allocation')}")
    print(f"       Content Pillars : {strategy.get('content_pillars')}")

    # -------------------------------------------------------------
    # STEP 2: GET /campaigns & GET /campaign/{campaign_id}
    # -------------------------------------------------------------
    print("\n[STEP 2] Fetching campaigns via GET /campaigns & GET /campaign/{id} ...")
    res_list = client.get("/campaigns")
    print(f"  --> GET /campaigns HTTP Status: {res_list.status_code}")
    assert res_list.status_code == 200, f"GET /campaigns failed: {res_list.text}"
    campaigns = res_list.json()
    assert isinstance(campaigns, list) and len(campaigns) >= 1
    print(f"  [OK] Successfully listed {len(campaigns)} campaign(s) from persistent store.")

    res_single = client.get(f"/campaign/{campaign_id}")
    print(f"  --> GET /campaign/{campaign_id} HTTP Status: {res_single.status_code}")
    assert res_single.status_code == 200, f"GET /campaign/{campaign_id} failed: {res_single.text}"
    fetched_campaign = res_single.json()
    assert fetched_campaign["id"] == campaign_id
    assert fetched_campaign["name"] == campaign["name"]
    print(f"  [OK] Verified Campaign Fetch by ID: {fetched_campaign['name']} (ID: {fetched_campaign['id']})")

    # -------------------------------------------------------------
    # STEP 3: POST /content/generate
    # -------------------------------------------------------------
    print("\n[STEP 3] Triggering Multi-Agent Content Studio via POST /content/generate ...")
    gen_payload = {
        "campaign_id": campaign_id,
        "topic": "Autumn Handcrafted Leather Gift Box Release & VIP Member Perks",
        "platforms": ["meta", "instagram", "tiktok", "xiaohongshu", "douyin", "wechat"],
        "count_per_platform": 1,
        "media_links": [
            "https://images.unsplash.com/photo-1548036328-c9fa89d128fa",
        ],
    }

    res_step3 = client.post("/content/generate", gen_payload)
    print(f"  --> HTTP Status: {res_step3.status_code}")
    assert res_step3.status_code == 200, f"Step 3 failed: {res_step3.text}"

    drafts = res_step3.json()
    assert isinstance(drafts, list) and len(drafts) >= 1
    print(f"  [OK] Generated {len(drafts)} drafts across target channels & languages:")
    for idx, d in enumerate(drafts[:4], 1):
        print(f"       Draft #{idx} [{d.get('platform').upper()} | {d.get('language').upper()}]: {d.get('title')}")
        print(f"                Snippet: {d.get('body')[:75]}...")
        print(f"                Tags   : {' '.join(d.get('hashtags', [])[:4])}")
    if len(drafts) > 4:
        print(f"       ... and {len(drafts) - 4} more drafts generated.")

    draft_ids = [d["id"] for d in drafts]

    # -------------------------------------------------------------
    # STEP 4: POST /publish/schedule
    # -------------------------------------------------------------
    print("\n[STEP 4] Executing publish and schedule tasks via POST /publish/schedule ...")
    # Immediate publish for first 3 drafts (with mock/demo fallback)
    pub_now_ids = draft_ids[:3]
    sched_ids = draft_ids[3:]

    pub_now_payload = {
        "content_draft_ids": pub_now_ids,
        "publish_now": True,
    }
    res_pub = client.post("/publish/schedule", pub_now_payload)
    print(f"  --> Immediate Publish HTTP Status: {res_pub.status_code}")
    assert res_pub.status_code == 200, f"Immediate publish failed: {res_pub.text}"
    published_tasks = res_pub.json()
    assert len(published_tasks) == len(pub_now_ids)
    print(f"  [OK] {len(published_tasks)} tasks published immediately (with mock fallback):")
    for t in published_tasks:
        print(f"       • Platform: {t.get('platform'):<12} Status: {t.get('status'):<10} Post ID: {t.get('external_post_id')}")

    # Queued schedule for remaining drafts
    sched_payload = {
        "content_draft_ids": sched_ids,
        "publish_now": False,
        "scheduled_at": "2026-09-22T19:30:00Z",
    }
    res_sched = client.post("/publish/schedule", sched_payload)
    print(f"  --> Scheduled Queue HTTP Status: {res_sched.status_code}")
    assert res_sched.status_code == 200, f"Scheduled queue failed: {res_sched.text}"
    scheduled_tasks = res_sched.json()
    assert len(scheduled_tasks) == len(sched_ids)
    print(f"  [OK] {len(scheduled_tasks)} tasks queued with target schedule: 2026-09-22T19:30:00Z")

    # -------------------------------------------------------------
    # STEP 5: GET /analytics/report
    # -------------------------------------------------------------
    print("\n[STEP 5] Generating cross-channel analytics report via GET /analytics/report ...")
    res_step5 = client.get("/analytics/report?period_days=30")
    print(f"  --> HTTP Status: {res_step5.status_code}")
    assert res_step5.status_code == 200, f"Step 5 failed: {res_step5.text}"

    report = res_step5.json()
    totals = report.get("totals", {})
    channels = report.get("channels", [])

    print(f"  [OK] Analytics Report Compiled Successfully:")
    print(f"       Channels Analyzed: {len(channels)}")
    print(f"       Total Impressions: {totals.get('impressions', 0):,}")
    print(f"       Total Clicks     : {totals.get('clicks', 0):,} ({totals.get('ctr_percentage')}% CTR)")
    print(f"       Ad Spend         : ${totals.get('spend', 0):,.2f} USD")
    print(f"       Estimated Revenue: ${totals.get('estimated_revenue', 0):,.2f} USD")
    print(f"       Campaign ROI     : +{totals.get('roi_percentage')}%")
    print(f"       Published Posts  : {totals.get('published_posts')}")
    print(f"       Scheduled Posts  : {totals.get('scheduled_posts')}")

    recs = report.get("recommendations", [])
    if recs:
        print("       AI Recommendations:")
        for r in recs[:2]:
            print(f"         * {r}")

    # -------------------------------------------------------------
    # PERSISTENCE VERIFICATION: Direct SQLite Database Check
    # -------------------------------------------------------------
    print("\n" + "-" * 70)
    print("  DATA PERSISTENCE VERIFICATION (Direct SQLite Store Inspection)")
    print("-" * 70)

    db_store = SQLiteStore("./data/marketing.db")
    persisted_campaign = db_store.get_campaign(campaign_id)
    assert persisted_campaign is not None, f"Campaign {campaign_id} must exist in SQLite database"
    assert persisted_campaign.id == campaign_id

    counts = db_store.counts()
    print(f"  [OK] Direct SQLite File Verified at ./data/marketing.db")
    print(f"       Persisted Campaigns in DB: {counts.get('campaigns')}")
    print(f"       Persisted Drafts in DB   : {counts.get('drafts')}")
    print(f"       Persisted Tasks in DB    : {counts.get('tasks')}")
    print(f"       Target Campaign Verified : '{persisted_campaign.name}' matches ID {campaign_id}")

    # -------------------------------------------------------------
    # FINAL SUMMARY
    # -------------------------------------------------------------
    print("\n" + "=" * 70)
    print("  ALL 5 WORKFLOW STEPS + DATA PERSISTENCE VERIFIED SUCCESSFULLY!")
    print("=" * 70)
    print("  [PASS] Step 1: POST /campaign/create          -> HTTP 200 OK")
    print("  [PASS] Step 2: GET /campaigns & GET /campaign -> HTTP 200 OK")
    print("  [PASS] Step 3: POST /content/generate         -> HTTP 200 OK")
    print("  [PASS] Step 4: POST /publish/schedule         -> HTTP 200 OK")
    print("  [PASS] Step 5: GET /analytics/report          -> HTTP 200 OK")
    print("  [PASS] Persistence: Data intact in SQLite     -> VERIFIED OK")
    print("=" * 70)


if __name__ == "__main__":
    try:
        run_e2e_workflow()
    except AssertionError as ae:
        print(f"\n[ASSERTION FAILED]: {ae}")
        sys.exit(1)
    except Exception as exc:
        print(f"\n[UNEXPECTED ERROR]: {exc}")
        sys.exit(1)
