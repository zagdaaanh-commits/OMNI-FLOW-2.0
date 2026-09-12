import os
import sys
import time
import subprocess
import requests

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 5000
BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"


def wait_for_server(timeout=15):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            res = requests.get(f"{BASE_URL}/health", timeout=1)
            if res.status_code == 200:
                print(f"[OK] Server is up and responding on {BASE_URL}/health")
                return True
        except Exception:
            time.sleep(0.5)
    return False


def run_pipeline_check():
    print("=" * 60)
    print(f"STARTING PIPELINE SELF-CHECK ON PORT {SERVER_PORT}")
    print("=" * 60)

    # 1. Health check & CORS
    print("\n1. Testing /health & CORS headers...")
    res = requests.get(f"{BASE_URL}/health", headers={"Origin": "http://localhost:3000"})
    assert res.status_code == 200, f"Health check failed with {res.status_code}"
    cors_origin = res.headers.get("access-control-allow-origin")
    print(f"   Status: 200 OK, CORS Origin Header: {cors_origin}")
    assert cors_origin in ["*", "http://localhost:3000"], "CORS header missing or invalid"

    # 2. Dashboard UI
    print("\n2. Testing Web Dashboard (/) & (/dashboard)...")
    res_ui = requests.get(f"{BASE_URL}/")
    assert res_ui.status_code == 200, f"Dashboard failed with {res_ui.status_code}"
    assert "OmniFlow AI" in res_ui.text, "Dashboard HTML content mismatch"
    print("   Status: 200 OK, Dashboard rendered successfully")

    # 3. Create Campaign (Standard Payload)
    print("\n3. Testing /campaign/create (Standard Multi-Platform Payload)...")
    camp_payload = {
        "name": "Global Cross-Border E-Commerce Launch",
        "objective": "Drive awareness and conversion across China & International channels",
        "budget": 1200.0,
        "currency": "USD",
        "platforms": ["meta", "instagram", "tiktok", "xiaohongshu", "douyin", "wechat"],
        "schedule": {
            "start_at": "2026-09-15T00:00:00Z",
            "timezone": "UTC",
            "posting_times": ["11:30", "19:30"],
            "frequency_per_day": 2,
        },
        "languages": ["en", "zh"],
        "brand_voice": "modern, engaging, authentic",
        "product_description": "Artisanal accessories and luxury gift sets",
    }
    res_camp = requests.post(f"{BASE_URL}/campaign/create", json=camp_payload)
    assert res_camp.status_code == 200, f"Campaign creation failed: {res_camp.text}"
    campaign = res_camp.json()
    campaign_id = campaign["id"]
    print(f"   Status: 200 OK, Created Campaign ID: {campaign_id}")
    print(f"   Planner Strategy Allocation: {campaign['strategy']['budget_allocation']}")

    # 4. Create Campaign (Default Swagger Schema Fallback)
    print("\n4. Testing /campaign/create (Default Swagger schema with 'string' values)...")
    swagger_camp = {
        "name": "Swagger Fallback Campaign",
        "objective": "Test schema robustness",
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
    res_sw_camp = requests.post(f"{BASE_URL}/campaign/create", json=swagger_camp)
    assert res_sw_camp.status_code == 200, f"Swagger campaign failed: {res_sw_camp.text}"
    print("   Status: 200 OK (Swagger default handled cleanly)")

    # 5. List Campaigns
    print("\n5. Testing /campaigns...")
    res_list = requests.get(f"{BASE_URL}/campaigns")
    assert res_list.status_code == 200
    campaigns = res_list.json()
    print(f"   Status: 200 OK, Total Campaigns in DB: {len(campaigns)}")

    # 6. Generate Content (Multi-Agent Copywriter)
    print("\n6. Testing /content/generate across target platforms...")
    gen_payload = {
        "campaign_id": campaign_id,
        "topic": "Summer Gift Box Release with Early Bird Perks",
        "count_per_platform": 1,
        "media_links": ["https://images.unsplash.com/photo-1512436991641-6745cdb1723f"],
    }
    res_gen = requests.post(f"{BASE_URL}/content/generate", json=gen_payload)
    assert res_gen.status_code == 200, f"Content generation failed: {res_gen.text}"
    drafts = res_gen.json()
    print(f"   Status: 200 OK, Generated {len(drafts)} drafts across target channels & languages.")
    draft_ids = [d["id"] for d in drafts]

    # 7. Content Generate (Swagger Default Schema with 'string' URL)
    print("\n7. Testing /content/generate (Swagger default payload with media_links=['string'])...")
    res_sw_gen = requests.post(f"{BASE_URL}/content/generate", json={
        "campaign_id": "string",
        "topic": "string",
        "media_links": ["string"],
    })
    assert res_sw_gen.status_code == 200, f"Swagger content generate failed: {res_sw_gen.text}"
    print("   Status: 200 OK (Swagger content generate handled cleanly)")

    # 8. Publish Immediately (Mock Demo Fallback)
    print("\n8. Testing /publish/schedule (Immediate publish with mock fallback)...")
    pub_payload = {
        "content_draft_ids": draft_ids[:3],
        "publish_now": True,
    }
    res_pub = requests.post(f"{BASE_URL}/publish/schedule", json=pub_payload)
    assert res_pub.status_code == 200, f"Publish failed: {res_pub.text}"
    pub_tasks = res_pub.json()
    assert all(t["status"] == "published" for t in pub_tasks)
    print(f"   Status: 200 OK, {len(pub_tasks)} tasks published with simulated post IDs:")
    for t in pub_tasks:
        print(f"     • [{t['platform']}] status={t['status']} external_post_id={t['external_post_id']}")

    # 9. Schedule for Future
    print("\n9. Testing /publish/schedule (Future schedule in queue)...")
    sched_payload = {
        "content_draft_ids": draft_ids[3:],
        "publish_now": False,
        "scheduled_at": "2026-09-18T19:30:00Z",
    }
    res_sched = requests.post(f"{BASE_URL}/publish/schedule", json=sched_payload)
    assert res_sched.status_code == 200, f"Schedule failed: {res_sched.text}"
    sched_tasks = res_sched.json()
    assert all(t["status"] == "scheduled" for t in sched_tasks)
    print(f"   Status: 200 OK, {len(sched_tasks)} tasks scheduled successfully.")

    # 10. Analytics Report & ROI
    print("\n10. Testing /analytics/report (Key Metrics, Impressions, Clicks, Spend, ROI)...")
    res_analytics = requests.get(f"{BASE_URL}/analytics/report?period_days=30")
    assert res_analytics.status_code == 200, f"Analytics report failed: {res_analytics.text}"
    report = res_analytics.json()
    totals = report["totals"]
    print(f"   Status: 200 OK")
    print(f"   Channels Analyzed: {len(report['channels'])}")
    print(f"   Total Impressions: {totals.get('impressions'):,}")
    print(f"   Total Clicks: {totals.get('clicks'):,} ({totals.get('ctr_percentage')}% CTR)")
    print(f"   Total Spend: ${totals.get('spend'):,.2f} USD")
    print(f"   Estimated Revenue: ${totals.get('estimated_revenue'):,.2f} USD")
    print(f"   Campaign ROI: +{totals.get('roi_percentage')}%")
    print(f"   AI Recommendations: {len(report['recommendations'])} items")

    print("\n" + "=" * 60)
    print("ALL PIPELINE CHECKS PASSED SUCCESSFULLY WITH STATUS 200 OK!")
    print("=" * 60)


if __name__ == "__main__":
    # Start server as subprocess on port 5000
    server_process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", SERVER_HOST, "--port", str(SERVER_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        if not wait_for_server(timeout=15):
            print("[ERROR] Server failed to start on port 5000 within timeout.")
            server_process.terminate()
            sys.exit(1)

        run_pipeline_check()

    finally:
        print("\nStopping server process...")
        server_process.terminate()
        server_process.wait(timeout=5)
        print("[OK] Server stopped cleanly.")
