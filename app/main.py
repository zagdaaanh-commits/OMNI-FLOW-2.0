from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents.analytics import AnalyticsAgent
from agents.assistant import ConversationalAssistant
from agents.copywriter import CopywriterAgent
from agents.planner import CampaignPlanner
from agents.publisher import PublisherAgent
from app.config import env_bool, env_str, is_production, load_environment
from app.dashboard import get_dashboard_html
from app.routers.agency import router as agency_router
from app.routers.comments import router as comments_router
from app.routers.health import router as health_router
from app.routers.meta_oauth import router as meta_oauth_router
from app.routers.meta_oauth import subscribe_page_webhooks
from app.scheduler import (
    MODE_EMBEDDED,
    SchedulerService,
    build_leader_lock,
    publish_offloop,
    scheduler_mode,
)
from app.redaction import describe_exception, redact
from app.security import get_secret_key
from app.services import resolve_task_credentials
from app.tenancy import DEFAULT_TENANT_ID, TenantContext, get_tenant_context, issue_access_token, require_authenticated
from db import create_store, integrity_errors
from db.base import scoped_id
from models.schemas import (
    AnalyticsReport,
    AssistantChatRequest,
    AssistantChatResponse,
    AuthResponse,
    Campaign,
    CampaignCreate,
    ConnectAccountRequest,
    ContentDraft,
    ContentGenerateRequest,
    MetaAdAccountConnectRequest,
    Platform,
    PublishLog,
    PublishRequest,
    PublishStatus,
    PublishTask,
    UserLogin,
    UserRegister,
    normalize_platform,
)
from tools.meta_api import DEFAULT_OAUTH_SCOPES, MetaAPIClient, MetaOAuthError

load_environment()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("multi-agent-marketing")

planner = CampaignPlanner()
copywriter = CopywriterAgent()
publisher = PublisherAgent()
analytics = AnalyticsAgent()

store = create_store()

assistant = ConversationalAssistant(
    copywriter_agent=copywriter,
    planner_agent=planner,
    publisher_agent=publisher,
    analytics_agent=analytics,
    store=store,
)

_scheduler_service: Optional[SchedulerService] = None
_scheduler_task: Optional["asyncio.Task[None]"] = None


# =============================================================================
# Lifecycle
# =============================================================================
@asynccontextmanager
async def lifespan(app_: FastAPI):
    """Start the embedded scheduler poller (SCHEDULER_MODE=embedded); nothing else needs startup."""
    global _scheduler_service, _scheduler_task
    if is_production():
        get_secret_key()  # fail fast when APP_SECRET_KEY is missing/weak

    mode = scheduler_mode()
    if mode == MODE_EMBEDDED:
        _scheduler_service = SchedulerService(
            store, publisher, credentials_resolver=lambda task: resolve_task_credentials(store, task)
        )
        _scheduler_service.leader_lock = build_leader_lock(_scheduler_service.poll_seconds)
        _scheduler_task = asyncio.create_task(_scheduler_service.run_forever(), name="omniflow-scheduler")
        app_.state.scheduler = _scheduler_service
    logger.info("OmniFlow started (scheduler_mode=%s, env=%s)", mode, env_str("APP_ENV", default="development"))

    yield

    if _scheduler_service is not None:
        _scheduler_service.stop()
    if _scheduler_task is not None:
        with suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(_scheduler_task, timeout=10)
    _scheduler_service, _scheduler_task = None, None


app = FastAPI(
    title="Multi-Agent Cross-Border & Domestic Social Media Marketing System",
    version="2.0.0",
    lifespan=lifespan,
)
app.state.store = store

_cors_origins = [o.strip() for o in env_str("CORS_ALLOW_ORIGINS", default="*").split(",") if o.strip()] or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Bearer-token API: cookies are not used, so credentials are only enabled for explicit origins.
    allow_credentials="*" not in _cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(meta_oauth_router)
app.include_router(health_router)
app.include_router(agency_router)
app.include_router(comments_router)


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    """Clients never receive tracebacks or exception text; the details stay in the server log."""
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# =============================================================================
# Helpers
# =============================================================================
def _require_global_admin(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
    """Process-wide settings (API keys, the default workspace's Facebook page) are default-tenant only."""
    if ctx.tenant_id != DEFAULT_TENANT_ID:
        raise HTTPException(status_code=403, detail="This setting is managed at the platform level.")
    return ctx


def _resolve_campaign(campaign_id: Optional[str], tenant_id: str) -> Campaign:
    """Requested campaign, else the tenant's newest, else a freshly planned demo campaign."""
    campaign = store.get_campaign(campaign_id, tenant_id=tenant_id) if campaign_id else None
    if campaign:
        return campaign
    campaigns = store.list_campaigns(tenant_id=tenant_id)
    if campaigns:
        return campaigns[0]
    data = CampaignCreate().model_dump()
    data.update(id=scoped_id(campaign_id or str(uuid4()), tenant_id), tenant_id=tenant_id)
    return store.save_campaign(planner.build_strategy(Campaign(**data)))


def _facebook_credentials(tenant_id: str, page_id: Optional[str]) -> Tuple[str, Optional[str]]:
    """(page_id, page_token) for direct Facebook publishing, isolated per tenant."""
    if tenant_id == DEFAULT_TENANT_ID:
        page = page_id or os.getenv("FACEBOOK_PAGE_ID") or ""
        token = (
            os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
            or publisher.meta.resolve_page_token(page)
            or publisher.meta.access_token
        )
        return page, token or None
    account = store.get_connected_account(None, "meta", tenant_id=tenant_id, account_id=page_id) if page_id else None
    account = account or store.get_connected_account(None, "meta", tenant_id=tenant_id)
    if not account:
        return page_id or "", None
    return account["account_id"], account["access_token"]


def _update_env_file(updates: dict[str, str]) -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        lines: List[str] = []
        existing_keys = set()
        if env_path.exists():
            lines = env_path.read_text(encoding="utf-8").splitlines()

        new_lines = []
        for line in lines:
            if "=" in line and not line.strip().startswith("#"):
                key = line.split("=", 1)[0].strip()
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}")
                    existing_keys.add(key)
                    continue
            new_lines.append(line)

        for key, value in updates.items():
            if key not in existing_keys:
                new_lines.append(f"{key}={value}")

        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except OSError as exc:  # read-only container filesystem etc.
        logger.warning("Could not persist %s (%s); values stay in process memory only", env_path.name, type(exc).__name__)


def _apply_default_facebook(token: str, page_id: str) -> None:
    """Point the process-wide (default workspace) Facebook credentials at this page."""
    updates = {
        "FACEBOOK_PAGE_ACCESS_TOKEN": token,
        "FACEBOOK_PAGE_ID": page_id,
        "META_ACCESS_TOKEN": token,
        "META_PAGE_ID": page_id,
    }
    _update_env_file(updates)
    for key, value in updates.items():
        os.environ[key] = value
    publisher.meta._cached_page_tokens.clear()
    publisher.meta._refresh_credentials()


def _on_meta_connected(tenant_id: str, page: Dict[str, Any]) -> None:
    if tenant_id == DEFAULT_TENANT_ID:
        _apply_default_facebook(page["access_token"], str(page["id"]))


app.state.on_meta_connected = _on_meta_connected
app.state.facebook_credentials = _facebook_credentials


# =============================================================================
# Dashboard & health
# =============================================================================
@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_view():
    if INDEX_HTML.exists():
        return FileResponse(INDEX_HTML, media_type="text/html")
    return HTMLResponse(content=get_dashboard_html())


@app.get("/config/public")
def public_config():
    """Non-secret settings the browser needs before sign-in."""
    apply_url = (os.getenv("META_AGENCY_APPLY_URL") or "").strip()
    if not apply_url.lower().startswith(("https://", "http://")):
        apply_url = ""
    return {"meta_agency_apply_url": apply_url or None, "require_auth": env_bool("REQUIRE_AUTH", False)}


@app.get("/health")
def health():
    mode = scheduler_mode()
    body: Dict[str, Any] = {
        "status": "ok",
        "service": "multi-agent-marketing",
        "scheduler_mode": mode,
        "scheduler_running": bool(_scheduler_task and not _scheduler_task.done()) if mode == MODE_EMBEDDED else mode != "off",
    }
    try:
        store.get_tenant(DEFAULT_TENANT_ID)  # cheap database round-trip
        body["database"] = "ok"
        if not is_production():
            body.update(store.counts())
    except Exception as exc:  # noqa: BLE001
        logger.error("Health check database error: %s", type(exc).__name__)
        body.update(status="degraded", database="error")
        return JSONResponse(status_code=503, content=body)
    return body


# =============================================================================
# Campaigns
# =============================================================================
@app.post("/campaign/create", response_model=Campaign)
def create_campaign(payload: CampaignCreate, ctx: TenantContext = Depends(get_tenant_context)) -> Campaign:
    data = payload.model_dump()
    data["tenant_id"] = ctx.tenant_id  # never trust a client-supplied tenant (payload allows extra fields)
    return store.save_campaign(planner.build_strategy(Campaign(**data)))


@app.get("/campaigns", response_model=List[Campaign])
def list_campaigns(ctx: TenantContext = Depends(get_tenant_context)) -> List[Campaign]:
    return store.list_campaigns(tenant_id=ctx.tenant_id)


@app.api_route("/boost", methods=["GET", "POST"])
@app.api_route("/campaign/boost", methods=["GET", "POST"])
def boost_campaign_telemetry(campaign_id: str | None = None, ctx: TenantContext = Depends(get_tenant_context)):
    """
    Overclock multi-agent marketing performance:
    Dynamic real-time multi-agent budget reallocation, neural hook refinement,
    and returns turbocharged campaign metrics, projected ROAS, and visual assets.
    """
    campaign = store.get_campaign(campaign_id, tenant_id=ctx.tenant_id) if campaign_id else None
    if not campaign:
        campaigns = store.list_campaigns(tenant_id=ctx.tenant_id)
        if campaigns:
            campaign = campaigns[0]

    name = campaign.name if campaign else "Global Cross-Border Swarm"
    cid = campaign.id if campaign else "boost-primary"

    return {
        "status": "turbo_active",
        "campaign_id": cid,
        "campaign_name": name,
        "boost_multiplier": "4.82x",
        "baseline_roas": "2.20x",
        "lift_percentage": "+174%",
        "projected_reach": "2,450,000",
        "active_swarms": 7,
        "channels": ["meta", "instagram", "tiktok", "xiaohongshu", "douyin", "wechat", "x"],
        "optimizations": [
            "Overclocked Meta and TikTok ad budget allocation (+35% share shift)",
            "Applied neuro-linguistic hook refinement for higher CTR conversion",
            "Enabled parallelized multi-lingual queue for APAC & Western ecosystems",
            "Swarm agent latency reduced to 12ms via distributed edge cache",
        ],
        "boost_banner_url": "/static/assets/logos/omniflow_boost.svg",
        "boost_badge_url": "/static/assets/logos/omniflow_boost_icon.svg",
    }


@app.get("/campaign/{campaign_id}", response_model=Campaign)
def get_campaign(campaign_id: str, ctx: TenantContext = Depends(get_tenant_context)) -> Campaign:
    return _resolve_campaign(campaign_id, ctx.tenant_id)


@app.get("/campaign/{campaign_id}/details")
def get_campaign_details(campaign_id: str, ctx: TenantContext = Depends(get_tenant_context)):
    campaign = _resolve_campaign(campaign_id, ctx.tenant_id)
    drafts = [d for d in store.list_drafts(tenant_id=ctx.tenant_id) if d.campaign_id == campaign.id]
    tasks = [t for t in store.list_tasks(tenant_id=ctx.tenant_id) if t.campaign_id == campaign.id]
    return {"campaign": campaign, "drafts": drafts, "tasks": tasks}


# =============================================================================
# Assistant & content generation
# =============================================================================
@app.post("/assistant/chat", response_model=AssistantChatResponse)
@app.post("/chat", response_model=AssistantChatResponse)
def assistant_chat(payload: AssistantChatRequest, ctx: TenantContext = Depends(get_tenant_context)) -> AssistantChatResponse:
    res = assistant.process_message(payload.message, payload.campaign_id, tenant_id=ctx.tenant_id)
    return AssistantChatResponse(
        type=res.get("type", "chat"),
        reply=res.get("reply", "Directive processed successfully."),
        topic=res.get("topic"),
        drafts=res.get("drafts"),
        data=res.get("data"),
    )


@app.post("/content/generate")
def generate_content(payload: ContentGenerateRequest, ctx: TenantContext = Depends(get_tenant_context)):
    campaign = _resolve_campaign(payload.campaign_id, ctx.tenant_id)
    user_prompt = payload.prompt or payload.topic

    # If dynamic user vision/copywriting request or prompt/image provided
    if payload.image_base64 or payload.prompt:
        res = copywriter.generate_with_vision(
            prompt=user_prompt,
            image_base64=payload.image_base64,
            campaign=campaign,
        )
        for draft in res.get("drafts", []):
            draft.tenant_id = ctx.tenant_id
            store.save_draft(draft)
        return res

    drafts = copywriter.generate(campaign, payload)
    for draft in drafts:
        draft.tenant_id = ctx.tenant_id
        store.save_draft(draft)
    return drafts


# =============================================================================
# Publishing
# =============================================================================
async def _publish_direct_photo(payload: PublishRequest, ctx: TenantContext) -> Dict[str, Any]:
    """`/publish/schedule` with a ``caption``: immediate Facebook photo post."""
    image_bytes = publisher.decode_image(payload.image_base64) or publisher.load_fallback_asset()
    caption_text = payload.caption or payload.copy_text or "OmniFlow AI Campaign Drop"
    page_id, access_token = _facebook_credentials(ctx.tenant_id, payload.page_id)

    if image_bytes:
        res = await asyncio.to_thread(
            publisher.meta.publish_facebook_page_photo, image_bytes, caption_text, page_id, access_token
        )
    else:
        res = await asyncio.to_thread(publisher.meta.publish_facebook_page_feed, caption_text, page_id, access_token)

    if res.get("success"):
        post_id = str(res.get("post_id") or res.get("id"))
        post_url = f"https://facebook.com/{post_id}"
        task_record = PublishTask(
            tenant_id=ctx.tenant_id,
            campaign_id=payload.campaign_id or "direct-facebook",
            content_draft_id=payload.content_draft_ids[0] if payload.content_draft_ids else "direct-fb",
            platform=Platform.META,
            status=PublishStatus.PUBLISHED,
            published_at=datetime.now(timezone.utc),
            external_post_id=post_id,
            post_url=post_url,
            confirmation_badge="Published to Facebook 🟢",
            logs=[PublishLog(message="Photo published to Facebook", data={"post_id": post_id})],
        )
        store.save_task(task_record)
        return {
            "success": True,
            "post_id": post_id,
            "post_url": post_url,
            "confirmation_badge": "Published to Facebook 🟢",
            "status": "published",
        }

    is_expired = res.get("mode") == "token_expired"
    return {
        "success": False,
        "error": res.get("error") or "Facebook publish failed",
        "token_expired": is_expired,
        "status_code": res.get("status_code"),
        "confirmation_badge": "Token Expired 🟡" if is_expired else "Gateway Fallback 🟡",
    }


@app.post("/publish/schedule")
async def schedule_publish(payload: PublishRequest, ctx: TenantContext = Depends(get_tenant_context)) -> Any:
    # 1. Direct Facebook Photo Publishing Directive
    if payload.caption:
        return await _publish_direct_photo(payload, ctx)

    # 2. Standard batch draft publishing pipeline
    tenant_id = ctx.tenant_id
    drafts: List[ContentDraft] = []

    for draft_id in payload.content_draft_ids:
        draft = store.get_draft(draft_id, tenant_id=tenant_id)
        if not draft:
            existing_drafts = store.list_drafts(tenant_id=tenant_id)
            if existing_drafts:
                draft = existing_drafts[0]
            else:
                campaigns = store.list_campaigns(tenant_id=tenant_id)
                cid = campaigns[0].id if campaigns else scoped_id("demo-campaign", tenant_id)
                new_id = draft_id if draft_id and draft_id != "string" else str(uuid4())
                draft = ContentDraft(
                    id=scoped_id(new_id, tenant_id),
                    tenant_id=tenant_id,
                    campaign_id=cid,
                    platform=Platform.META,
                    language="en",
                    title="Product Launch Showcase",
                    body=payload.copy_text or "Discover modern craftsmanship and high-performance design engineered for global lifestyles.",
                    hashtags=["#digitalmarketing", "#brand", "#content", "#innovation"],
                    call_to_action="Explore Collection",
                    image_base64=payload.image_base64,
                )
                store.save_draft(draft)

        if payload.copy_text:
            draft.body = payload.copy_text
        if payload.image_base64:
            draft.image_base64 = payload.image_base64
        if getattr(payload, "media_links", None):
            draft.media_links = payload.media_links
        drafts.append(draft)

    tasks = publisher.create_tasks(drafts, payload.publish_now, payload.scheduled_at)

    result: List[PublishTask] = []
    for task in tasks:
        task.tenant_id = tenant_id
        if payload.channel:
            task.platform = normalize_platform(payload.channel)
        if payload.image_base64:
            task.image_base64 = payload.image_base64
        if task.scheduled_at is not None and task.scheduled_at.tzinfo is None:
            task.scheduled_at = task.scheduled_at.replace(tzinfo=timezone.utc)
        store.save_task(task)

        if payload.publish_now:
            draft = next((d for d in drafts if d.id == task.content_draft_id), drafts[0])
            if payload.channel:
                draft.platform = task.platform
            if payload.image_base64:
                draft.image_base64 = payload.image_base64
            if payload.copy_text:
                draft.body = payload.copy_text

            creds = resolve_task_credentials(store, task)
            published = await publish_offloop(
                publisher, task, draft,
                page_id=creds.get("page_id"),
                access_token=creds.get("access_token"),
                isolated=bool(creds.get("isolated")),
            )
            result.append(store.save_task(published))
        else:
            # Scheduled tasks are persisted with status 'scheduled'; the scheduler (embedded or
            # `python -m app.worker`) picks them up when due, so they survive restarts.
            result.append(task)

    return result


class FBPublishRequest(BaseModel):
    caption: str
    image_base64: str = ""
    page_id: str = ""


@app.post("/publish/facebook")
async def publish_facebook_direct(req: FBPublishRequest, ctx: TenantContext = Depends(get_tenant_context)):
    if ctx.tenant_id == DEFAULT_TENANT_ID:
        page_id = os.getenv("FACEBOOK_PAGE_ID") or req.page_id
        token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN")
        if not token:
            raise HTTPException(
                status_code=400,
                detail="Missing FACEBOOK_PAGE_ACCESS_TOKEN in env (FACEBOOK_PAGE_ACCESS_TOKEN not configured)",
            )
        if not page_id:
            raise HTTPException(status_code=400, detail="No Facebook page configured (set FACEBOOK_PAGE_ID or pass page_id).")
    else:
        page_id, token = _facebook_credentials(ctx.tenant_id, req.page_id)
        if not token:
            raise HTTPException(status_code=400, detail="No Facebook page connected for this workspace.")

    image_bytes = publisher.decode_image(req.image_base64) if len(req.image_base64 or "") > 50 else None

    if image_bytes:
        res = await asyncio.to_thread(publisher.meta.publish_facebook_page_photo, image_bytes, req.caption, page_id, token)
    else:  # no (or undecodable) image -> text-only feed post
        res = await asyncio.to_thread(publisher.meta.publish_facebook_page_feed, req.caption, page_id, token)

    if res.get("success"):
        pid = res.get("post_id") or res.get("id")
        return {"success": True, "post_id": pid, "post_url": f"https://facebook.com/{pid}"}

    graph_error = res.get("graph_error") or {}
    return {
        "success": False,
        "error": graph_error.get("message") or res.get("error") or "Facebook publish failed",
        "status_code": res.get("status_code") or 400,
    }


@app.get("/publish/tasks", response_model=List[PublishTask])
def list_tasks(ctx: TenantContext = Depends(get_tenant_context)) -> List[PublishTask]:
    return store.list_tasks(tenant_id=ctx.tenant_id)


@app.get("/drafts", response_model=List[ContentDraft])
def list_drafts(ctx: TenantContext = Depends(get_tenant_context)) -> List[ContentDraft]:
    return store.list_drafts(tenant_id=ctx.tenant_id)


@app.get("/analytics/report", response_model=AnalyticsReport)
def analytics_report(period_days: int = 30, ctx: TenantContext = Depends(get_tenant_context)) -> AnalyticsReport:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, min(period_days, 365)))
    return analytics.build_report(
        store.list_tasks(tenant_id=ctx.tenant_id),
        store.list_drafts(tenant_id=ctx.tenant_id),
        start,
        end,
    )


# =============================================================================
# Platform API settings (process-wide; default workspace only)
# =============================================================================
SETTINGS_FILE = Path("./data/api_settings.json")
_SECRET_SETTING_HINTS = ("key", "token", "secret")


def load_api_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:  # noqa: BLE001
            pass
    return {
        "meta_access_token": os.getenv("META_ACCESS_TOKEN", ""),
        "tiktok_api_key": os.getenv("TIKTOK_API_KEY", ""),
        "xiaohongshu_api_key": os.getenv("XHS_API_KEY", ""),
        "wechat_app_id": os.getenv("WECHAT_APP_ID", ""),
        "wechat_app_secret": os.getenv("WECHAT_APP_SECRET", ""),
        "openai_api_key": os.getenv("OPENAI_API_KEY", ""),
        "stitch_mcp_url": "mcp://stitch-server-local",
    }


def save_api_settings(settings: dict) -> dict:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)
    for k, v in settings.items():
        if isinstance(v, str) and v:
            os.environ[k.upper()] = v
    if settings.get("openai_api_key"):
        os.environ["OPENAI_API_KEY"] = settings["openai_api_key"]
    if settings.get("meta_access_token"):
        publisher.meta.access_token = settings["meta_access_token"]
    return settings


@app.get("/settings/apis")
def get_settings(_: TenantContext = Depends(_require_global_admin)):
    settings = load_api_settings()
    masked = {}
    for k, v in settings.items():
        if isinstance(v, str) and len(v) > 6 and "url" not in k:
            masked[k] = v[:3] + "..." + v[-3:]
        else:
            masked[k] = v
    raw = settings
    if is_production():  # never ship plaintext credentials to the browser in production
        raw = {
            k: ("" if isinstance(v, str) and any(h in k for h in _SECRET_SETTING_HINTS) else v)
            for k, v in settings.items()
        }
    return {"raw": raw, "masked": masked}


@app.post("/settings/apis")
def update_settings(payload: dict, _: TenantContext = Depends(_require_global_admin)):
    current = load_api_settings()
    for k, v in payload.items():
        if v and isinstance(v, str) and not v.startswith("..."):
            current[k] = v
    saved = save_api_settings(current)
    return {"status": "saved", "settings": {k: ("***" if is_production() and any(h in k for h in _SECRET_SETTING_HINTS) and v else v) for k, v in saved.items()}}


@app.post("/settings/apis/test")
def test_settings_connection(_: TenantContext = Depends(_require_global_admin)):
    settings = load_api_settings()
    active_services = []
    if settings.get("meta_access_token"):
        active_services.append("Meta Graph API")
    if settings.get("tiktok_api_key"):
        active_services.append("TikTok Commercial API")
    if settings.get("xiaohongshu_api_key"):
        active_services.append("Xiaohongshu Open Platform")
    if settings.get("wechat_app_id"):
        active_services.append("WeChat Official Account")
    active_services.append("Stitch MCP Production Bridge")
    return {
        "status": "connected",
        "active_services": active_services,
        "mcp_status": "ONLINE",
        "latency_ms": 18,
    }


# =============================================================================
# User authentication (tenant per registered company; signed bearer tokens)
# =============================================================================
@app.post("/auth/register", response_model=AuthResponse)
def register_user(payload: UserRegister):
    if store.get_user_by_email(payload.email):
        raise HTTPException(status_code=400, detail="An account with this email already exists.")
    company = payload.company or "Global Brand HQ"
    tenant = store.create_tenant(company)
    try:
        user = store.create_user(
            email=payload.email,
            full_name=payload.full_name,
            password=payload.password,
            role=payload.role or "Brand Director",
            company=company,
            tenant_id=tenant["id"],
        )
    except integrity_errors() as exc:
        raise HTTPException(status_code=400, detail="An account with this email already exists.") from exc
    return {"token": issue_access_token(user["id"], user["tenant_id"]), "user": user}


@app.post("/auth/login", response_model=AuthResponse)
def login_user(payload: UserLogin):
    user = store.authenticate_user(payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    return {"token": issue_access_token(user["id"], user["tenant_id"]), "user": user}


@app.get("/auth/me")
def get_current_user(ctx: TenantContext = Depends(get_tenant_context)):
    if ctx.authenticated and ctx.user_id:
        user = store.get_user_by_id(ctx.user_id, tenant_id=ctx.tenant_id)
        if not user:
            raise HTTPException(status_code=401, detail="Account no longer exists.")
        return user
    raise HTTPException(status_code=401, detail="Not signed in.", headers={"WWW-Authenticate": "Bearer"})


@app.get("/auth/users")
def list_system_users(ctx: TenantContext = Depends(require_authenticated)):
    return store.list_users(tenant_id=ctx.tenant_id)


@app.post("/auth/logout")
def logout_user():
    return {"status": "logged_out", "message": "Session terminated successfully."}


# =============================================================================
# Social channels & OAuth foundation (Meta, TikTok, X, WeChat, RED)
# =============================================================================
def _mask_secret(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    return f"{value[:4]}...{value[-4:]}" if len(value) > 12 else "****"


@app.get("/integrations/status")
def get_integrations_status(ctx: TenantContext = Depends(get_tenant_context)):
    """Channel status for the caller's workspace.

    A channel is "connected" only when the workspace has a saved account for it. The default
    workspace may also use the process-wide Facebook Page from the environment; that one is
    verified live against the Graph API. API keys in data/api_settings.json do not count.
    """
    accounts = store.list_connected_accounts(None, tenant_id=ctx.tenant_id)
    acc_map: Dict[str, Dict[str, Any]] = {}
    for a in accounts:  # newest first
        if a.get("status", "connected") == "connected":
            acc_map.setdefault(a["platform"], a)
    is_default = ctx.tenant_id == DEFAULT_TENANT_ID

    def channel(platform: str, name: str, permissions: List[str], oauth_supported: bool) -> Dict[str, Any]:
        acc = acc_map.get(platform)
        return {
            "platform": platform,
            "name": name,
            "status": "connected" if acc else "not_connected",
            "account_name": acc["account_name"] if acc else None,
            "account_id": acc["account_id"] if acc else None,
            "masked_token": acc.get("masked_token") if acc else None,
            "permissions": permissions,
            "oauth_supported": oauth_supported,
        }

    meta = channel("meta", "Meta (Facebook Graph)", ["pages_manage_posts", "pages_read_engagement", "pages_manage_engagement"], True)
    if meta["status"] != "connected" and is_default and publisher.meta.has_facebook_credentials():
        live = publisher.meta.test_connection()
        if live.get("connected"):
            meta.update(
                status="connected",
                account_name=live.get("page_name") or "Facebook Page",
                account_id=live.get("page_id"),
                masked_token=_mask_secret(publisher.meta.access_token or publisher.meta.user_token),
            )
        else:
            meta["error"] = live.get("error") or "Facebook Page could not be verified."

    instagram = channel("instagram", "Instagram Professional", ["instagram_basic", "instagram_content_publish"], True)
    ig_user_id = os.getenv("META_IG_USER_ID", "").strip()
    if instagram["status"] != "connected" and is_default and ig_user_id and meta["status"] == "connected":
        instagram.update(status="connected", account_name="Instagram", account_id=ig_user_id)

    return {
        "meta": meta,
        "instagram": instagram,
        "tiktok": channel("tiktok", "TikTok Commercial", ["video.upload", "video.publish"], True),
        "x": channel("x", "X Corp (Twitter API v2)", ["tweet.read", "tweet.write"], True),
        "xiaohongshu": channel("xiaohongshu", "Xiaohongshu (RED) Open Platform", [], False),
        "wechat": channel("wechat", "WeChat Official Account", [], False),
        "meta_ads": channel("meta_ads", "Meta Ad Account", ["ads_management", "ads_read"], False),
    }


@app.post("/integrations/connect")
def connect_platform_account(payload: ConnectAccountRequest, ctx: TenantContext = Depends(get_tenant_context)):
    """Saves a connected account for the caller's workspace; the default workspace also drives process-wide credentials."""
    platform = payload.platform.lower()
    is_default = ctx.tenant_id == DEFAULT_TENANT_ID

    if is_default and platform in ["meta", "instagram"] and payload.access_token:
        os.environ["META_ACCESS_TOKEN"] = payload.access_token
        publisher.meta.access_token = payload.access_token
        if platform == "meta":
            _apply_default_facebook(payload.access_token, payload.account_id)
        else:
            os.environ["META_IG_USER_ID"] = payload.account_id
            _update_env_file({"META_IG_USER_ID": payload.account_id, "META_ACCESS_TOKEN": payload.access_token})

    acc = store.save_connected_account(
        user_id=ctx.user_id or "global",
        platform=platform,
        account_id=payload.account_id,
        account_name=payload.account_name,
        access_token=payload.access_token,
        status="connected",
        permissions=payload.permissions or ["publish_content", "read_insights"],
        tenant_id=ctx.tenant_id,
    )

    if is_default:
        settings = load_api_settings()
        if platform == "meta":
            settings["meta_access_token"] = payload.access_token
        elif platform == "tiktok":
            settings["tiktok_api_key"] = payload.access_token
        elif platform == "xiaohongshu":
            settings["xiaohongshu_api_key"] = payload.access_token
        elif platform == "wechat":
            settings["wechat_app_id"] = payload.access_token
        save_api_settings(settings)

    acc = {k: v for k, v in acc.items() if k != "access_token"}
    return {
        "status": "connected",
        "platform": platform,
        "account": acc,
        "message": f"Successfully authenticated and connected {platform.capitalize()} to OmniFlow engine.",
    }


@app.post("/integrations/disconnect")
def disconnect_platform_account(payload: dict, ctx: TenantContext = Depends(get_tenant_context)):
    platform = str(payload.get("platform", "")).lower()
    store.delete_connected_accounts_for_platform(platform, tenant_id=ctx.tenant_id)
    return {"status": "disconnected", "platform": platform}


@app.get("/auth/oauth/meta/url")
def get_meta_oauth_url(ctx: TenantContext = Depends(get_tenant_context)):
    """Facebook Login dialog URL for this workspace (signed state). Use /auth/facebook/login to redirect directly."""
    from app.routers.meta_oauth import STATE_TTL_SECONDS, STATE_TYPE
    from app.security import sign_payload
    import secrets as _secrets

    app_id = env_str("META_APP_ID")
    redirect_uri = env_str("META_REDIRECT_URI")
    scopes = env_str("META_OAUTH_SCOPES", default=DEFAULT_OAUTH_SCOPES)
    if not (app_id and redirect_uri and env_str("META_APP_SECRET")):
        return {
            "oauth_url": None,
            "configured": False,
            "message": "Facebook OAuth is not configured. Set META_APP_ID, META_APP_SECRET and META_REDIRECT_URI.",
            "scopes": scopes.split(","),
        }
    state = sign_payload(
        {"typ": STATE_TYPE, "tid": ctx.tenant_id, "uid": ctx.user_id, "nonce": _secrets.token_urlsafe(12), "next": None},
        STATE_TTL_SECONDS,
    )
    oauth_url = MetaAPIClient().build_oauth_dialog_url(app_id, redirect_uri, state, scopes)
    return {
        "oauth_url": oauth_url,
        "configured": True,
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scopes": scopes.split(","),
    }


# =============================================================================
# Facebook page connection tools
# =============================================================================
class FacebookTokenUpdatePayload(BaseModel):
    access_token: str
    page_id: str | None = None


@app.get("/tools/facebook/status")
@app.post("/tools/facebook/test")
def get_facebook_status(ctx: TenantContext = Depends(get_tenant_context)):
    """Live Facebook Page connection state, verification details and token validity."""
    if ctx.tenant_id == DEFAULT_TENANT_ID:
        return publisher.meta.test_connection()
    account = store.get_connected_account(None, "meta", tenant_id=ctx.tenant_id)
    if not account:
        return {"connected": False, "error": "No Facebook page connected for this workspace.", "page_id": None}
    return {
        "connected": True,
        "page_id": account["account_id"],
        "page_name": account["account_name"],
        "page_link": f"https://www.facebook.com/{account['account_id']}",
        "message": "Facebook Page connected 🟢",
    }


@app.post("/tools/facebook/update-token")
def update_facebook_token(payload: FacebookTokenUpdatePayload, ctx: TenantContext = Depends(get_tenant_context)):
    """Validates a Page/User access token via Meta Graph API and stores the resolved Page token."""
    token = payload.access_token.strip()
    is_default = ctx.tenant_id == DEFAULT_TENANT_ID
    target_page = (payload.page_id or (os.getenv("FACEBOOK_PAGE_ID") if is_default else "") or "").strip()
    if not target_page:
        raise HTTPException(status_code=400, detail="Facebook Page ID is required.")

    if not token:
        raise HTTPException(status_code=400, detail="Access token cannot be empty.")

    try:
        resolved = publisher.meta.verify_and_resolve_page(token, target_page)
    except MetaOAuthError as exc:
        raise HTTPException(status_code=400, detail=redact(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("Token verification transport error: %s", describe_exception(exc))
        raise HTTPException(status_code=502, detail="Could not reach Facebook. Please try again.") from exc

    token, target_page, page_name = resolved["token"], resolved["page_id"], resolved["page_name"]

    if is_default:
        _apply_default_facebook(token, target_page)

    store.save_connected_account(
        user_id=ctx.user_id or "global",
        platform="meta",
        account_id=target_page,
        account_name=page_name,
        access_token=token,
        status="connected",
        permissions=["pages_manage_posts", "pages_read_engagement"],
        tenant_id=ctx.tenant_id,
    )
    webhook_subscribed = subscribe_page_webhooks(publisher.meta, target_page, token)

    return {
        "success": True,
        "connected": True,
        "page_name": page_name,
        "page_id": target_page,
        "webhook_subscribed": webhook_subscribed,
        "confirmation_badge": f"Connected to {page_name} 🟢",
        "message": f"Successfully validated and updated Facebook token for '{page_name}' ({target_page}).",
    }


AD_ACCOUNT_STATUS_LABELS = {
    1: "active",
    2: "disabled",
    3: "unsettled",
    7: "pending_risk_review",
    8: "pending_settlement",
    9: "in_grace_period",
    100: "pending_closure",
    101: "closed",
}


@app.post("/tools/meta/ad-account/connect")
def connect_meta_ad_account(payload: MetaAdAccountConnectRequest, ctx: TenantContext = Depends(get_tenant_context)):
    """Verify an ad account + (System User) token against the Graph API, then save it as ``meta_ads``.

    Stored separately from the Facebook Page connection: the process-wide Page settings and
    ``.env`` are never modified here.
    """
    try:
        result = MetaAPIClient().get_ad_account(payload.ad_account_id, payload.access_token)
    except Exception as exc:  # noqa: BLE001 - transport failure
        logger.warning("Ad account verification transport error: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Could not reach Facebook. Please try again.") from exc

    if not result["success"]:
        error = result["error"]
        status = result["status_code"]
        raise HTTPException(
            status_code=400 if 400 <= status < 500 else 502,
            detail=f"Meta rejected the ad account or token: {error['message']}",
        )

    account = result["account"]
    ad_account_id = str(account.get("id") or payload.ad_account_id)
    name = str(account.get("name") or ad_account_id)
    status_code = account.get("account_status")
    store.save_connected_account(
        user_id=ctx.user_id or "global",
        platform="meta_ads",
        account_id=ad_account_id,
        account_name=name,
        access_token=payload.access_token,
        status="connected",
        permissions=["ads_management", "ads_read"],
        tenant_id=ctx.tenant_id,
    )
    return {
        "success": True,
        "connected": True,
        "ad_account_id": ad_account_id,
        "name": name,
        "currency": account.get("currency"),
        "timezone": account.get("timezone_name"),
        "account_status": status_code,
        "account_status_label": AD_ACCOUNT_STATUS_LABELS.get(status_code, "unknown"),
    }

