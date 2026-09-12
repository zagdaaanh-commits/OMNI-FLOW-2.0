from datetime import datetime, timezone

from agents.copywriter import CopywriterAgent
from agents.planner import CampaignPlanner
from models.schemas import Campaign, CampaignCreate, ContentGenerateRequest, Platform, ScheduleWindow, TargetAudience


def make_campaign() -> Campaign:
    payload = CampaignCreate(
        name="Test campaign",
        objective="awareness",
        target_audience=TargetAudience(locations=["Global"]),
        budget=100,
        platforms=[Platform.INSTAGRAM, Platform.XIAOHONGSHU],
        schedule=ScheduleWindow(start_at=datetime.now(timezone.utc), posting_times=["12:00"]),
        languages=["en", "zh"],
    )
    return CampaignPlanner().build_strategy(Campaign(**payload.model_dump()))


def test_planner_allocates_budget_and_platform_plan():
    campaign = make_campaign()
    assert campaign.strategy["budget_allocation"]["instagram"] == 50
    assert "xiaohongshu" in campaign.strategy["platform_plan"]


def test_copywriter_generates_multiplatform_content():
    campaign = make_campaign()
    drafts = CopywriterAgent().generate(
        campaign,
        ContentGenerateRequest(campaign_id=campaign.id, topic="summer product", count_per_platform=1),
    )
    assert len(drafts) == 4
    assert {d.platform for d in drafts} == {Platform.INSTAGRAM, Platform.XIAOHONGSHU}
    assert all(d.body and d.hashtags for d in drafts)
