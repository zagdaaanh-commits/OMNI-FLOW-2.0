from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator, AliasChoices

# Shared tenant used for single-merchant / demo mode and for rows created before multi-tenancy.
DEFAULT_TENANT_ID = "default"


class Platform(str, Enum):
    META = "meta"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    X = "x"
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"
    WECHAT = "wechat"


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    PLANNED = "planned"
    ACTIVE = "active"
    COMPLETED = "completed"
    PAUSED = "paused"


class PublishStatus(str, Enum):
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"


def normalize_platform(val: Any) -> Platform:
    if isinstance(val, Platform):
        return val
    s = str(val).strip().lower()
    mapping = {
        "facebook": Platform.META,
        "meta": Platform.META,
        "instagram": Platform.INSTAGRAM,
        "ig": Platform.INSTAGRAM,
        "tiktok": Platform.TIKTOK,
        "x": Platform.X,
        "twitter": Platform.X,
        "xiaohongshu": Platform.XIAOHONGSHU,
        "red": Platform.XIAOHONGSHU,
        "douyin": Platform.DOUYIN,
        "wechat": Platform.WECHAT,
        "weixin": Platform.WECHAT,
    }
    return mapping.get(s, Platform.META)


class TargetAudience(BaseModel):
    model_config = ConfigDict(extra="allow")

    age_range: Optional[str] = None
    genders: List[str] = Field(default_factory=list)
    locations: List[str] = Field(default_factory=list)
    interests: List[str] = Field(default_factory=list)
    languages: List[str] = Field(default_factory=lambda: ["en"])
    persona: Optional[str] = None


class ScheduleWindow(BaseModel):
    model_config = ConfigDict(extra="allow")

    start_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    end_at: Optional[datetime] = None
    timezone: str = "UTC"
    posting_times: List[str] = Field(default_factory=lambda: ["12:00"], description="HH:MM local times")
    frequency_per_day: int = Field(default=1, ge=1, le=24)

    @field_validator("posting_times", mode="before")
    @classmethod
    def sanitize_posting_times(cls, v: Any) -> List[str]:
        if not v or not isinstance(v, list):
            return ["12:00"]
        cleaned = []
        for item in v:
            s = str(item).strip()
            if ":" in s:
                parts = s.split(":")
                if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                    cleaned.append(f"{int(parts[0]):02d}:{int(parts[1]):02d}")
                    continue
            cleaned.append("12:00")
        return cleaned or ["12:00"]


class CampaignCreate(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str = Field(default="Global Growth Campaign", min_length=1, max_length=200)
    objective: str = Field(default="Increase product awareness and conversions across target channels", min_length=1, max_length=500)
    target_audience: TargetAudience = Field(default_factory=TargetAudience)
    budget: float = Field(default=1000.0, ge=0)
    currency: str = "USD"
    platforms: List[Platform] = Field(
        default_factory=lambda: [Platform.META, Platform.INSTAGRAM, Platform.TIKTOK, Platform.XIAOHONGSHU]
    )
    schedule: ScheduleWindow = Field(default_factory=ScheduleWindow)
    languages: List[str] = Field(default_factory=lambda: ["en"])
    brand_voice: str = "clear, helpful, modern"
    product_description: str = "A modern cross-border gift and fashion collection"

    @field_validator("platforms", mode="before")
    @classmethod
    def validate_platforms(cls, v: Any) -> List[Platform]:
        if not v:
            return [Platform.META, Platform.INSTAGRAM, Platform.TIKTOK, Platform.XIAOHONGSHU]
        if isinstance(v, (str, Platform)):
            v = [v]
        return [normalize_platform(p) for p in v]

    @field_validator("languages", mode="before")
    @classmethod
    def validate_languages(cls, v: Any) -> List[str]:
        if not v or not isinstance(v, list):
            return ["en"]
        res = [str(item) for item in v if str(item).strip()]
        return res or ["en"]


class Campaign(CampaignCreate):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str = DEFAULT_TENANT_ID
    status: CampaignStatus = CampaignStatus.DRAFT
    strategy: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ContentGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    campaign_id: str = "demo-campaign"
    topic: str = Field(
        default="Product showcase",
        validation_alias=AliasChoices("topic", "topic_prompt", "prompt", "message", "query"),
    )
    prompt: Optional[str] = None
    image_base64: Optional[str] = None
    platforms: Optional[List[Platform]] = None
    count_per_platform: int = Field(default=1, ge=1, le=20)
    media_links: List[str] = Field(default_factory=list)

    @field_validator("platforms", mode="before")
    @classmethod
    def validate_platforms(cls, v: Any) -> Optional[List[Platform]]:
        if v is None:
            return None
        if isinstance(v, (str, Platform)):
            v = [v]
        return [normalize_platform(p) for p in v]

    @field_validator("media_links", mode="before")
    @classmethod
    def validate_media_links(cls, v: Any) -> List[str]:
        if not v or not isinstance(v, list):
            return []
        return [str(x) for x in v if str(x).strip()]


class ContentDraft(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str = DEFAULT_TENANT_ID
    campaign_id: str
    platform: Platform
    language: str
    title: Optional[str] = None
    body: str
    hashtags: List[str] = Field(default_factory=list)
    media_links: List[str] = Field(default_factory=list)
    image_base64: Optional[str] = None
    call_to_action: Optional[str] = None
    scheduled_at: Optional[datetime] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("media_links", mode="before")
    @classmethod
    def validate_media_links(cls, v: Any) -> List[str]:
        if not v or not isinstance(v, list):
            return []
        return [str(x) for x in v if str(x).strip()]


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    content_draft_ids: List[str] = Field(default_factory=lambda: ["mock-draft-1"])
    publish_now: bool = True
    scheduled_at: Optional[datetime] = None
    campaign_id: Optional[str] = None
    channel: Optional[str] = None
    platform: Optional[str] = None
    image_base64: Optional[str] = None
    copy_text: Optional[str] = None
    caption: Optional[str] = None
    page_id: Optional[str] = None
    media_links: List[str] = Field(default_factory=list)



    @model_validator(mode="before")
    @classmethod
    def handle_request_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "immediate" in data and "publish_now" not in data:
                data["publish_now"] = bool(data["immediate"])
        return data

    @field_validator("content_draft_ids", mode="before")
    @classmethod
    def validate_draft_ids(cls, v: Any) -> List[str]:
        if not v:
            return ["mock-draft-1"]
        if isinstance(v, str):
            return [v]
        res = [str(x) for x in v if str(x).strip()]
        return res or ["mock-draft-1"]


class PublishLog(BaseModel):
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    level: str = "INFO"
    message: str
    data: Dict[str, Any] = Field(default_factory=dict)


class PublishTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str = DEFAULT_TENANT_ID
    campaign_id: str
    content_draft_id: str
    platform: Platform
    status: PublishStatus
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    external_post_id: Optional[str] = None
    post_url: Optional[str] = None
    confirmation_badge: Optional[str] = None
    image_base64: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0
    updated_at: Optional[datetime] = None
    logs: List[PublishLog] = Field(default_factory=list)


class StructuredContentResponse(BaseModel):
    # The JSON key stays "copy"; the attribute is renamed so it does not shadow BaseModel.copy().
    model_config = ConfigDict(extra="allow", protected_namespaces=(), populate_by_name=True, serialize_by_alias=True)
    copy_text: str = Field(validation_alias=AliasChoices("copy", "copy_text"), serialization_alias="copy")
    hashtags: Any
    platforms: List[str] = Field(default_factory=lambda: ["Meta", "TikTok"])
    draft_id: Optional[str] = None
    image_base64: Optional[str] = None
    drafts: Optional[List[ContentDraft]] = None


class ChannelMetrics(BaseModel):
    platform: Platform
    period_start: datetime
    period_end: datetime
    impressions: int = 0
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    clicks: int = 0
    followers_gained: int = 0
    engagements: int = 0
    spend: float = 0
    currency: str = "USD"

    @field_validator("impressions", "views", "likes", "comments", "shares", "clicks", "followers_gained", "engagements")
    @classmethod
    def non_negative(cls, value: int) -> int:
        return max(0, value)


class AnalyticsReport(BaseModel):
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    period_start: datetime
    period_end: datetime
    channels: List[ChannelMetrics] = Field(default_factory=list)
    totals: Dict[str, Any] = Field(default_factory=dict)
    recommendations: List[str] = Field(default_factory=list)
    anomalies: List[str] = Field(default_factory=list)

class UserRegister(BaseModel):
    model_config = ConfigDict(extra="allow")
    email: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=4, max_length=100)
    full_name: str = Field(..., min_length=1, max_length=100)
    company: Optional[str] = Field(default="Global Brand HQ")
    role: Optional[str] = Field(default="Brand Director")


class UserLogin(BaseModel):
    model_config = ConfigDict(extra="allow")
    email: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=1)


class UserProfile(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    company: str
    avatar_url: Optional[str] = ""
    created_at: str
    tenant_id: Optional[str] = None


class Tenant(BaseModel):
    id: str
    name: str
    slug: Optional[str] = None
    plan: str = "free"
    status: str = "active"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class AuthResponse(BaseModel):
    token: str
    user: UserProfile


class ConnectAccountRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    platform: str
    account_id: str
    account_name: str
    access_token: str
    permissions: Optional[List[str]] = None


class ConnectedAccountSchema(BaseModel):
    id: str
    platform: str
    account_id: str
    account_name: str
    status: str
    masked_token: str
    permissions: Optional[List[str]] = None
    updated_at: str


class AssistantChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: str
    campaign_id: Optional[str] = None
    conversation_id: Optional[str] = None


class AssistantChatResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: str = "chat"
    reply: str
    topic: Optional[str] = None
    drafts: Optional[List[ContentDraft]] = None
    data: Optional[Dict[str, Any]] = None

# --------------------------------------------------------------- agency intake
# Unified Social Credit Code (统一社会信用代码, GB 32100-2015): 18 characters drawn from
# digits and the uppercase letters except I, O, S, V, Z; the last one is a checksum.
_USCC_CHARS = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_USCC_WEIGHTS = [pow(3, i, 31) for i in range(17)]
_USCC_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{2}[0-9]{6}[0-9A-HJ-NPQRTUWXY]{10}$")


def is_valid_credit_code(code: str) -> bool:
    if not _USCC_RE.match(code):
        return False
    total = sum(_USCC_CHARS.index(ch) * w for ch, w in zip(code[:17], _USCC_WEIGHTS))
    return _USCC_CHARS[(31 - total % 31) % 31] == code[17]


class AgencyApplicationRequest(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=200)
    credit_code: str = Field(..., description="Unified Social Credit Code (营业执照代码), 18 characters")
    store_url: str = Field(..., max_length=500, description="Independent store / shop URL")
    contact: str = Field(..., min_length=1, max_length=200, description="WeChat / phone / email")
    remarks: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("company_name", "contact")
    @classmethod
    def _required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("credit_code")
    @classmethod
    def _credit_code(cls, value: str) -> str:
        value = value.strip().upper()
        if not is_valid_credit_code(value):
            raise ValueError("must be a valid 18-character Unified Social Credit Code")
        return value

    @field_validator("store_url")
    @classmethod
    def _store_url(cls, value: str) -> str:
        value = value.strip()
        if value and "://" not in value:
            value = "https://" + value
        parts = urlsplit(value)
        host = parts.hostname or ""
        if parts.scheme not in ("http", "https") or "." not in host:
            raise ValueError("must be an http(s) URL of the store, e.g. https://shop.example.com")
        return value

    @field_validator("remarks")
    @classmethod
    def _remarks(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        return value or None


# ------------------------------------------------------------- meta comments
class CommentReplyRequest(BaseModel):
    comment_id: str = Field(..., min_length=1, max_length=128)
    message: str = Field(..., min_length=1, max_length=8000)
    page_token: Optional[str] = None

    @field_validator("comment_id")
    @classmethod
    def _comment_id(cls, value: str) -> str:
        # Graph object ids are digits joined by "_"; anything else could redirect the POST to another edge.
        value = value.strip()
        if not re.fullmatch(r"[0-9]+(?:_[0-9]+)*", value):
            raise ValueError("must be a Graph API comment id such as 123_456")
        return value

    @field_validator("message")
    @classmethod
    def _message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("page_token")
    @classmethod
    def _page_token(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        return value or None
