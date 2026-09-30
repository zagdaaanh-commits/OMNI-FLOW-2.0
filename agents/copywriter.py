from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional
from uuid import uuid4

from agents.llm_client import (
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMNotConfiguredError,
    get_llm_client,
)
from app.config import load_environment
from models.schemas import Campaign, ContentDraft, ContentGenerateRequest, Platform

load_environment()

logger = logging.getLogger("multi-agent-marketing.copywriter")


class CopywriterAgent:
    """
    AI Copywriter Agent backed by the unified OpenAI-compatible LLM client
    (default: Zhipu GLM-4-Flash) with platform-specific prompt engineering,
    exponential-backoff retries and deterministic fallbacks that always return copy.
    """

    DEFAULT_MODEL = "glm-4-flash"

    PLATFORM_RULES: Dict[Platform, Dict[str, int]] = {
        Platform.INSTAGRAM: {"max_chars": 2200, "hashtags": 8},
        Platform.META: {"max_chars": 5000, "hashtags": 5},
        Platform.TIKTOK: {"max_chars": 4000, "hashtags": 6},
        Platform.X: {"max_chars": 280, "hashtags": 3},
        Platform.XIAOHONGSHU: {"max_chars": 1000, "hashtags": 8},
        Platform.DOUYIN: {"max_chars": 4000, "hashtags": 6},
        Platform.WECHAT: {"max_chars": 8000, "hashtags": 5},
    }

    PLATFORM_SYSTEM_PROMPTS: Dict[Platform, str] = {
        Platform.META: (
            "You are an expert direct-response copywriter for Meta/Facebook Feed Ads. "
            "Write high-converting ad copy with a scroll-stopping hook, 2-3 concise value propositions, "
            "and a compelling call to action. Keep tone professional yet energetic."
        ),
        Platform.TIKTOK: (
            "You are a viral TikTok creator and social ad strategist. "
            "Write dynamic, high-retention video caption copy with a 3-second hook that stops users mid-scroll. "
            "Include quick spoken-word style pacing and energetic tone."
        ),
        Platform.X: (
            "You are an influential tech, commerce, and brand strategist on X (formerly Twitter). "
            "Write a sharp, punchy, high-engagement post strictly under 240 characters. "
            "Focus on bold insights, clear value, and memorable wording."
        ),
        Platform.WECHAT: (
            "You are an editorial content strategist for WeChat Official Accounts. "
            "Write an authoritative, storytelling-driven overview highlighting product craft, "
            "exclusive VIP perks, and lifestyle impact."
        ),
        Platform.INSTAGRAM: (
            "You are a luxury lifestyle and visual brand storyteller on Instagram. "
            "Write an aesthetic, engaging caption with storytelling pacing, subtle emojis, "
            "and a clear invitation to shop or learn more."
        ),
        Platform.XIAOHONGSHU: (
            "You are a leading lifestyle tastemaker on Xiaohongshu (RED). "
            "Write an authentic, highly aesthetic personal recommendation note with practical tips "
            "and lifestyle aesthetics."
        ),
        Platform.DOUYIN: (
            "You are a premier short-video commerce strategist on Douyin. "
            "Write an attention-grabbing hook and fast-paced product showcase script."
        ),
    }

    PLATFORM_HOOKS: Dict[Platform, str] = {
        Platform.INSTAGRAM: "✨ Visual Spotlight: ",
        Platform.META: "What is your next move? ",
        Platform.TIKTOK: "Stop scrolling for 3 seconds: ",
        Platform.X: "Direct insight: ",
        Platform.XIAOHONGSHU: "📌 Essential Lifestyle Pick: ",
        Platform.DOUYIN: "🔥 Experience this today: ",
        Platform.WECHAT: "Exclusive Highlights & Deep Dive: ",
    }

    VISION_SYSTEM_PROMPT = (
        "You are an expert direct-response copywriter for Meta and TikTok feeds. "
        "Write high-converting ad copy with a scroll-stopping hook, concise value propositions, and a clear CTA. "
        "All generated copy must be in clean, professional English. "
        "You MUST respond ONLY with valid JSON with these exact keys: "
        "{\"copy\": \"...\", \"hashtags\": [\"#tag1\", \"#tag2\"], \"platforms\": [\"Meta\", \"TikTok\"]}"
    )

    def __init__(self) -> None:
        self.client: Optional[LLMClient] = None
        self.api_key: str = ""
        self.base_url: str = ""
        self.model: str = self.DEFAULT_MODEL
        self._init_openai_client()

    def _init_openai_client(self) -> None:
        """Refresh the shared LLM client (picks up runtime key/model/proxy changes)."""
        llm = get_llm_client()
        self.api_key = llm.settings.api_key
        self.base_url = llm.settings.base_url
        self.model = llm.settings.model
        self.client = llm if llm.is_configured else None

    @property
    def _llm_available(self) -> bool:
        return bool(self.client and self.api_key and not self.client.auth_failed)

    def _vision_model_for(self, has_image: bool) -> str:
        if has_image and self.client is not None:
            lowered = self.model.lower()
            if "glm" in lowered and "4v" not in lowered:
                return self.client.vision_model
        return self.model

    @staticmethod
    def _parse_json_reply(raw_text: str) -> Dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.MULTILINE).strip()
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                raise
            parsed = json.loads(match.group(0))
        if not isinstance(parsed, dict):
            raise ValueError("LLM reply is not a JSON object")
        return parsed

    def generate_with_vision(
        self,
        prompt: str,
        image_base64: Optional[str] = None,
        campaign: Optional[Campaign] = None,
    ) -> Dict[str, Any]:
        """
        Generates structured ad copy from a prompt and optional product image.
        Uses the vision model (glm-4v-flash by default) for images, falls back to a
        text-only call, then to the deterministic template generator.
        Returns: { copy, hashtags (space-joined str), platforms, draft_id, image_base64, drafts }
        """
        self._init_openai_client()
        prompt_text = (prompt or "").strip()
        result_copy: Optional[str] = None
        result_hashtags: List[str] = []
        result_platforms: List[str] = ["Meta", "TikTok"]
        generation_meta: Dict[str, Any] = {"agent": "CopywriterAgent", "model": self.model}

        if self._llm_available:
            assert self.client is not None
            text_part = {"type": "text", "text": f"Product/Idea: {prompt_text or 'High-Performance Showcase'}"}
            try:
                raw_text: Optional[str] = None
                if image_base64:
                    img_url = image_base64 if image_base64.startswith("data:") else f"data:image/jpeg;base64,{image_base64}"
                    vision_model = self._vision_model_for(True)
                    try:
                        raw_text = self.client.chat(
                            [
                                {"role": "system", "content": self.VISION_SYSTEM_PROMPT},
                                {"role": "user", "content": [text_part, {"type": "image_url", "image_url": {"url": img_url}}]},
                            ],
                            model=vision_model,
                            max_tokens=700,
                        )
                        generation_meta["model"] = vision_model
                    except LLMAuthError:
                        raise
                    except LLMError as vision_err:
                        logger.info("Vision call on %s failed (%s); retrying text-only on %s", vision_model, vision_err, self.model)
                if raw_text is None:
                    raw_text = self.client.chat(
                        [
                            {"role": "system", "content": self.VISION_SYSTEM_PROMPT},
                            {"role": "user", "content": text_part["text"]},
                        ],
                        max_tokens=700,
                    )
                parsed = self._parse_json_reply(raw_text)
                copy_val = parsed.get("copy")
                result_copy = str(copy_val).strip() if copy_val else None
                raw_tags = parsed.get("hashtags")
                if isinstance(raw_tags, list):
                    result_hashtags = [str(t).strip() for t in raw_tags if str(t).strip()]
                elif isinstance(raw_tags, str):
                    result_hashtags = [t.strip() for t in raw_tags.split() if t.strip()]
                raw_platforms = parsed.get("platforms")
                if isinstance(raw_platforms, list) and raw_platforms:
                    result_platforms = [str(p) for p in raw_platforms]
                elif isinstance(raw_platforms, dict) and raw_platforms:
                    result_platforms = [str(k) for k in raw_platforms.keys()]
                generation_meta["status"] = "ai_generated"
            except Exception as exc:  # noqa: BLE001 - always degrade gracefully
                logger.warning("Vision/LLM generation failed: %s. Using template fallback.", exc)
                generation_meta["status"] = "fallback"
                generation_meta["fallback_reason"] = type(exc).__name__
        else:
            generation_meta["status"] = "fallback"
            generation_meta["fallback_reason"] = "llm_auth_failed" if self.client else "llm_not_configured"

        if not result_copy:
            result_copy = (
                f"🔥 Introducing the next evolution in performance: {prompt_text}!\n\n"
                f"Engineered to redefine modern standards with effortless reliability, refined aesthetics, "
                f"and uncompromising craftsmanship built for discerning global lifestyles.\n\n"
                f"✨ Key Highlights:\n"
                f"• Precision-engineered architecture for maximum durability\n"
                f"• Streamlined intuitive experience from day one\n"
                f"• 4.9/5 verified global customer satisfaction rating\n\n"
                f"Tap below to secure your allocation today before first drop sells out!"
            )
            result_hashtags = ["#Innovation", "#LaunchDay", "#ProductDrop", "#SmartLiving", "#Ecommerce"]
            if generation_meta.get("status") == "ai_generated":
                generation_meta["status"] = "fallback"
                generation_meta["fallback_reason"] = "empty_copy"

        hashtags_str = " ".join(result_hashtags)

        cid = campaign.id if campaign else "demo-campaign"
        tenant_kwargs: Dict[str, Any] = {}
        if campaign is not None and getattr(campaign, "tenant_id", None):
            tenant_kwargs["tenant_id"] = campaign.tenant_id
        meta_draft = ContentDraft(
            id=str(uuid4()),
            campaign_id=cid,
            platform=Platform.META,
            language="en",
            title=f"{prompt_text[:30]} | Meta",
            body=result_copy,
            hashtags=list(result_hashtags),
            image_base64=image_base64,
            call_to_action="Learn More",
            metadata=dict(generation_meta),
            **tenant_kwargs,
        )
        first_line = result_copy.splitlines()[0] if result_copy else ""
        tiktok_draft = ContentDraft(
            id=str(uuid4()),
            campaign_id=cid,
            platform=Platform.TIKTOK,
            language="en",
            title=f"{prompt_text[:30]} | TikTok",
            body=f"Stop scrolling! ✋ Have you seen {prompt_text}?\n\n{first_line}\n\nCheck sound and tap link below ⬇️",
            hashtags=list(result_hashtags),
            image_base64=image_base64,
            call_to_action="TikTok Drop",
            metadata=dict(generation_meta),
            **tenant_kwargs,
        )

        return {
            "copy": result_copy,
            "hashtags": hashtags_str,
            "platforms": result_platforms,
            "draft_id": meta_draft.id,
            "image_base64": image_base64,
            "drafts": [meta_draft, tiktok_draft],
        }

    def generate(self, campaign: Campaign, request: ContentGenerateRequest) -> List[ContentDraft]:
        # Refresh client in case keys/model/proxy changed at runtime
        self._init_openai_client()

        platforms = request.platforms or campaign.platforms
        drafts: List[ContentDraft] = []
        schedule_hint = campaign.schedule.posting_times or ["12:00"]
        languages = campaign.languages or ["en"]

        for platform in platforms:
            for language in languages:
                for i in range(request.count_per_platform):
                    draft = self._generate_single_draft(
                        campaign=campaign,
                        topic=request.topic,
                        platform=platform,
                        language=language,
                        media_links=request.media_links,
                        schedule_time=schedule_hint[i % len(schedule_hint)],
                    )
                    drafts.append(draft)

        return drafts

    def _generate_single_draft(
        self,
        campaign: Campaign,
        topic: str,
        platform: Platform,
        language: str,
        media_links: List[str],
        schedule_time: str,
    ) -> ContentDraft:
        topic = topic or campaign.product_description or campaign.objective or "Product Showcase"
        body = None
        hashtags = None
        cta = None
        ai_metadata: Dict[str, Any] = {
            "agent": "CopywriterAgent",
            "provider": self.client.settings.provider_host if self.client else "none",
            "model": self.model,
            "schedule_hint": schedule_time,
        }

        if self._llm_available:
            try:
                ai_result = self._call_openai(campaign, topic, platform, language)
                if ai_result:
                    body = ai_result.get("body")
                    cta = ai_result.get("cta")
                    if ai_result.get("hashtags"):
                        hashtags = ai_result["hashtags"]
                    ai_metadata["status"] = "ai_generated"
            except LLMAuthError as auth_err:
                logger.warning("LLM authentication failed: %s. Further calls disabled for this key.", auth_err)
                ai_metadata["status"] = "simulated_fallback"
                ai_metadata["fallback_reason"] = "LLM authentication failed; used resilient localized fallback"
            except Exception as exc:  # noqa: BLE001 - always degrade gracefully
                logger.warning("LLM call (%s) failed: %s. Using resilient generation fallback.", self.model, exc)
                ai_metadata["status"] = "simulated_fallback"
                ai_metadata["fallback_reason"] = str(exc)[:300]
        else:
            auth_failed = bool(self.client and self.client.auth_failed)
            ai_metadata["status"] = "simulated_fallback" if auth_failed else "deterministic_fallback"
            ai_metadata["note"] = "LLM offline, unauthorized, or key omitted"

        if not body:
            body = self._write_fallback(campaign, topic, platform, language)
        if not cta:
            cta = self._cta_fallback(platform, language)
        if not hashtags:
            hashtags = self._hashtags(campaign, platform, topic)

        limit = self.PLATFORM_RULES[platform]["max_chars"]
        body = body[:limit]

        extra: Dict[str, Any] = {}
        if getattr(campaign, "tenant_id", None):
            extra["tenant_id"] = campaign.tenant_id
        return ContentDraft(
            id=str(uuid4()),
            campaign_id=campaign.id,
            platform=platform,
            language=language,
            title=self._title(topic, platform, language),
            body=body,
            hashtags=hashtags,
            media_links=media_links,
            call_to_action=cta,
            metadata=ai_metadata,
            **extra,
        )

    def _call_openai(self, campaign: Campaign, topic: str, platform: Platform, language: str) -> Optional[Dict]:
        """
        Executes a chat completion via the unified LLM client (retries/backoff included).
        Returns {"body", "cta", "hashtags"}; raises LLMError subclasses on failure.
        """
        if self.client is None:
            raise LLMNotConfiguredError("LLM client not configured")

        system_prompt = self.PLATFORM_SYSTEM_PROMPTS.get(
            platform,
            "You are a professional cross-border social media copywriter."
        )

        user_prompt = (
            f"Campaign Name: {campaign.name}\n"
            f"Brand Voice: {campaign.brand_voice}\n"
            f"Product/Topic: {topic}\n"
            f"Target Platform: {platform.value.upper()}\n"
            f"Target Language: {language}\n\n"
            f"Please write:\n"
            f"1. A compelling post body tailored specifically for {platform.value.upper()}.\n"
            f"2. A short call to action (under 6 words).\n"
            f"3. 3-5 high-converting relevant hashtags."
        )

        content = self.client.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=600,
            temperature=0.7,
        )

        found_tags = re.findall(r"#\w+", content)
        clean_body = re.sub(r"#\w+", "", content).strip()

        return {
            "body": clean_body if clean_body else content,
            "cta": self._cta_fallback(platform, language),
            "hashtags": found_tags if found_tags else self._hashtags(campaign, platform, topic),
        }

    def _write_fallback(self, campaign: Campaign, topic: str, platform: Platform, language: str) -> str:
        topic_clean = topic.strip() or "Modern Innovation"

        if language.startswith("zh"):
            if platform in (Platform.XIAOHONGSHU, Platform.DOUYIN):
                return (
                    f"终于被我挖到了！✨ {topic_clean} 真的太懂生活美学了！\n\n"
                    f"高颜值与硬核实用性双重在线，摆在桌上随手一拍都是大片质感。"
                    f"强烈推荐给近期有品质提升需求的小伙伴们～\n\n"
                    f"欢迎在评论区交流体验感受哦！"
                )
            elif platform == Platform.WECHAT:
                return (
                    f"【官方首发】探索全新 {topic_clean} 的设计美学与硬核科技。\n\n"
                    f"秉持极致工艺与创新体验，专为追求卓越品质的用户量身打造。"
                    f"点击下方阅读原文，即刻预约专属礼遇与体验名额。"
                )
            return f"探索全新 {topic_clean}。以现代极简工艺与卓越性能，为全球用户带来非凡体验。点击了解详情。"

        # English (Default)
        if platform == Platform.META:
            return (
                f"🔥 Introducing the breakthrough in everyday performance: {topic_clean}!\n\n"
                f"Engineered to redefine modern standards with effortless reliability, sophisticated aesthetics, "
                f"and uncompromising craftsmanship built for demanding lifestyles.\n\n"
                f"✨ Key Highlights:\n"
                f"• Precision-engineered architecture for maximum durability\n"
                f"• Streamlined intuitive experience from day one\n"
                f"• 4.9/5 verified global customer satisfaction rating\n\n"
                f"Tap below to secure your launch allocation with complimentary express dispatch."
            )
        elif platform == Platform.INSTAGRAM:
            return (
                f"✨ Elevate your creative sanctuary with {topic_clean}.\n\n"
                f"Where architectural minimalism meets acoustic precision. Designed to restore natural balance "
                f"and modern focus to your daily workflow.\n\n"
                f"Save this post & tap the link in bio to explore the limited release collection."
            )
        elif platform == Platform.TIKTOK:
            return (
                f"Stop scrolling for 3 seconds! ✋ Have you actually tried {topic_clean} yet?\n\n"
                f"This single upgrade completely transformed our setup. Zero distractions, 100% pure focus. "
                f"You literally have to experience this in person!\n\n"
                f"Tap the shopping link below before the first drop sells out ⬇️"
            )
        elif platform == Platform.X:
            return (
                f"Direct release: {topic_clean} is officially live.\n\n"
                f"High-throughput architecture, zero clutter, built for global operators who demand higher standards.\n\n"
                f"Full breakdown & allocation: [link]"
            )
        elif platform == Platform.WECHAT:
            return (
                f"【Curated Spotlight】 The Architectural Story of {topic_clean}.\n\n"
                f"Balancing artisanal craftsmanship with next-generation materials for discerning global audiences.\n"
                f"Discover our private reservation catalogue via official link below."
            )
        elif platform == Platform.XIAOHONGSHU:
            return (
                f"📌 Essential Aesthetic Find: {topic_clean}!\n\n"
                f"Unmatched minimalist textures and understated luxury that instantly elevates your creative environment.\n"
                f"Drop a comment below with your favorite colorway!"
            )
        else:
            return (
                f"Experience {topic_clean}. Engineered with premium materials and ergonomic precision for global creators. "
                f"Explore full specifications and reserve yours today."
            )

    def _title(self, topic: str, platform: Platform, language: str) -> str:
        topic = topic or "Campaign update"
        return f"{topic} | {platform.value.upper()}"

    def _hashtags(self, campaign: Campaign, platform: Platform, topic: str) -> List[str]:
        raw = [topic.replace(" ", ""), campaign.name.replace(" ", ""), "Marketing", platform.value]
        tags = []
        for x in raw:
            if x and x not in tags:
                clean = re.sub(r"[^A-Za-z0-9_]", "", x)
                if clean:
                    tags.append("#" + clean)
        tags.extend(["#digitalmarketing", "#brand", "#innovation", "#ecommerce"])
        return tags[: self.PLATFORM_RULES[platform]["hashtags"]]

    @staticmethod
    def _cta_fallback(platform: Platform, language: str) -> str:
        if language.startswith("zh"):
            return "即刻探索并领取礼遇"
        if platform == Platform.TIKTOK:
            return "Shop TikTok Drop"
        if platform == Platform.INSTAGRAM:
            return "Link in Bio"
        if platform == Platform.X:
            return "Explore Release"
        return "Learn More"

