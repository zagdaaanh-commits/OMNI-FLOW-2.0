from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List

from models.schemas import Campaign, CampaignStatus


class CampaignPlanner:
    """Turns campaign inputs into a platform-aware execution strategy."""

    PLATFORM_BEST_PRACTICE = {
        "instagram": {"frequency": 1, "formats": ["reel", "carousel", "story"]},
        "meta": {"frequency": 1, "formats": ["feed", "reel", "story"]},
        "tiktok": {"frequency": 1, "formats": ["short_video"]},
        "x": {"frequency": 2, "formats": ["text", "image", "thread"]},
        "xiaohongshu": {"frequency": 1, "formats": ["note", "image", "short_video"]},
        "douyin": {"frequency": 1, "formats": ["short_video"]},
        "wechat": {"frequency": 1, "formats": ["article", "video"]},
    }

    def build_strategy(self, campaign: Campaign) -> Campaign:
        platform_plan: Dict[str, dict] = {}
        for platform in campaign.platforms:
            key = platform.value
            defaults = self.PLATFORM_BEST_PRACTICE.get(key, {"frequency": 1, "formats": ["feed", "short_video"]})
            platform_plan[key] = {
                "recommended_frequency_per_day": defaults["frequency"],
                "formats": defaults["formats"],
                "language_priority": campaign.languages,
                "audience": campaign.target_audience.model_dump(),
            }

        campaign.strategy = {
            "objective": campaign.objective,
            "budget_allocation": self._allocate_budget(campaign),
            "platform_plan": platform_plan,
            "content_pillars": self._content_pillars(campaign.objective, campaign.product_description),
            "measurement": ["views", "engagement_rate", "clicks", "followers_gained", "spend"],
        }
        campaign.status = CampaignStatus.PLANNED
        campaign.updated_at = datetime.now(timezone.utc)
        return campaign

    def _allocate_budget(self, campaign: Campaign) -> Dict[str, float]:
        if not campaign.platforms or campaign.budget <= 0:
            return {p.value: 0.0 for p in campaign.platforms}
        equal_share = campaign.budget / len(campaign.platforms)
        return {p.value: round(equal_share, 2) for p in campaign.platforms}

    @staticmethod
    def _content_pillars(objective: str, product_description: str) -> List[str]:
        base = ["education", "social proof", "product value", "community engagement"]
        if any(word in (objective + " " + product_description).lower() for word in ["sale", "sales", "revenue", "conversion"]):
            base.insert(0, "offer & conversion")
        return base

    def generate_schedule(self, campaign: Campaign, drafts_per_platform: int = 1) -> List[datetime]:
        start = campaign.schedule.start_at or datetime.now(timezone.utc)
        times = campaign.schedule.posting_times or ["12:00"]
        result: List[datetime] = []
        for i in range(drafts_per_platform):
            time_str = times[i % len(times)]
            try:
                parts = time_str.split(":")
                hh, mm = int(parts[0]), int(parts[1])
            except Exception:
                hh, mm = 12, 0
            day = start + timedelta(days=i // max(1, len(times)))
            result.append(day.replace(hour=hh, minute=mm, second=0, microsecond=0))
        return result
