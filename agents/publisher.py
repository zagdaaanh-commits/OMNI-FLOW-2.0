from __future__ import annotations

import base64
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from models.schemas import ContentDraft, Platform, PublishLog, PublishStatus, PublishTask
from tools.meta_api import MetaAPIClient
from tools.rpa_tool import RPATool

logger = logging.getLogger(__name__)

_FALLBACK_ASSETS = ("smart_device.jpg", "luxury_box.jpg", "nordic_lamp.jpg")


class PublisherAgent:
    """Publishes drafts and drives tasks through ``scheduled -> publishing -> published | failed``.

    Truthfulness rules:
    * A live Graph API success -> ``published`` with ``external_post_id`` and ``post_url``.
    * Credentials configured but Meta rejected the call (expired token, permissions, network)
      -> ``failed`` with ``error`` set; never reported as published.
    * No credentials (demo mode) or a simulated platform adapter -> ``published`` with
      ``mode=simulated`` recorded in the task log, exactly as the demo dashboard expects.
    """

    def __init__(self) -> None:
        self.meta = MetaAPIClient()
        self.rpa = RPATool()

    def create_tasks(self, drafts: List[ContentDraft], publish_now: bool, scheduled_at: datetime | None) -> List[PublishTask]:
        tasks: List[PublishTask] = []
        for draft in drafts:
            target_time = None if publish_now else (scheduled_at or draft.scheduled_at)
            status = PublishStatus.PUBLISHING if publish_now else PublishStatus.SCHEDULED
            tasks.append(PublishTask(
                tenant_id=draft.tenant_id,
                campaign_id=draft.campaign_id,
                content_draft_id=draft.id,
                platform=draft.platform,
                status=status,
                scheduled_at=target_time,
                logs=[PublishLog(message="Task created", data={"agent": "PublisherAgent"})],
            ))
        return tasks

    async def publish(
        self,
        task: PublishTask,
        draft: ContentDraft,
        *,
        page_id: Optional[str] = None,
        access_token: Optional[str] = None,
        isolated: bool = False,
    ) -> PublishTask:
        """Publish ``draft`` for ``task`` and return the updated task (never raises).

        ``page_id`` / ``access_token`` let the caller publish with a tenant's connected page
        instead of the process-wide credentials.  With ``isolated=True`` the process-wide
        credentials are NEVER used: a tenant without its own page gets a simulated publish.
        """
        task.status = PublishStatus.PUBLISHING
        task.logs.append(PublishLog(message="Publishing started", data={"platform": task.platform.value}))
        try:
            img_b64 = getattr(task, "image_base64", None) or getattr(draft, "image_base64", None)
            if task.platform in {Platform.META, Platform.INSTAGRAM}:
                result = self._publish_meta_or_instagram(
                    draft,
                    task_platform=task.platform,
                    image_base64=img_b64,
                    page_id=page_id,
                    access_token=access_token,
                    isolated=isolated,
                )
            elif task.platform in {Platform.X, Platform.TIKTOK}:
                result = self._publish_placeholder(draft, "Official API adapter simulated with mock fallback")
            else:
                result = await self._publish_rpa_placeholder(draft)
        except Exception as exc:  # noqa: BLE001 - convert to an explicit FAILED state
            logger.exception("Publish crashed for task %s (%s)", task.id, task.platform.value)
            task.status = PublishStatus.FAILED
            task.error = f"{type(exc).__name__}: {exc}"
            task.confirmation_badge = "Publish Failed 🔴"
            task.logs.append(PublishLog(
                level="ERROR",
                message="Publish failed with an unexpected error",
                data={"error": task.error, "platform": task.platform.value},
            ))
            return task

        if result.get("failed"):
            task.status = PublishStatus.FAILED
            task.error = str(result.get("error") or "Publish failed")
            task.confirmation_badge = result.get("confirmation_badge") or "Publish Failed 🔴"
            task.logs.append(PublishLog(
                level="ERROR",
                message="Publish failed",
                data={k: v for k, v in result.items() if k != "raw"},
            ))
            logger.error("Publish failed for task %s: %s", task.id, task.error)
            return task

        task.status = PublishStatus.PUBLISHED
        task.published_at = datetime.now(timezone.utc)
        task.error = None
        task.external_post_id = str(
            result.get("external_post_id") or result.get("id") or f"pub-{task.platform.value}-{task.id[:8]}"
        )
        if result.get("post_url"):
            task.post_url = result["post_url"]
        elif result.get("mode") == "live_facebook":
            task.post_url = f"https://www.facebook.com/{task.external_post_id}"
        if result.get("confirmation_badge"):
            task.confirmation_badge = result["confirmation_badge"]
        if result.get("error"):
            task.error = str(result["error"])
        task.logs.append(PublishLog(
            message="Publish succeeded" if result.get("mode") == "live_facebook" else "Publish completed (simulated)",
            data={k: v for k, v in result.items() if k != "raw"},
        ))
        return task

    # ------------------------------------------------------------- helpers
    @staticmethod
    def load_fallback_asset() -> Optional[bytes]:
        for name in _FALLBACK_ASSETS:
            path = os.path.join(os.getcwd(), "app", "static", "assets", "images", name)
            if os.path.isfile(path):
                try:
                    with open(path, "rb") as fh:
                        return fh.read()
                except OSError:
                    continue
        return None

    @staticmethod
    def decode_image(img_b64: Optional[str]) -> Optional[bytes]:
        if not img_b64:
            return None
        try:
            clean = img_b64.split(",", 1)[1] if "," in img_b64 else img_b64
            return base64.b64decode(clean.strip())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error decoding image base64: %s", type(exc).__name__)
            return None

    def _image_bytes_for(self, draft: ContentDraft, image_base64: Optional[str]) -> Optional[bytes]:
        img_b64 = image_base64 or getattr(draft, "image_base64", None)
        if not img_b64 and draft.media_links:
            img_b64 = next((m for m in draft.media_links if m and m.startswith("data:image")), None)
        raw = self.decode_image(img_b64)
        if raw:
            return raw
        for link in draft.media_links or []:
            if not link or link.startswith("http") or link.startswith("data:"):
                continue
            link_clean = link.lstrip("/").replace("\\", "/")
            for candidate in (link, os.path.join(os.getcwd(), link_clean), os.path.join(os.getcwd(), "app", link_clean)):
                if os.path.isfile(candidate):
                    try:
                        with open(candidate, "rb") as fh:
                            return fh.read()
                    except OSError:
                        continue
        # Facebook posts always carry a creative: fall back to a bundled showcase asset.
        return self.load_fallback_asset()

    def _publish_meta_or_instagram(
        self,
        draft: ContentDraft,
        task_platform: Optional[Platform] = None,
        image_base64: Optional[str] = None,
        page_id: Optional[str] = None,
        access_token: Optional[str] = None,
        isolated: bool = False,
    ) -> Dict:
        target_platform = task_platform or draft.platform

        if target_platform == Platform.META:
            message = draft.body
            if draft.hashtags:
                message += "\n\n" + " ".join(draft.hashtags)

            if isolated:
                has_creds = bool(page_id and access_token)
            else:
                has_creds = bool(page_id and access_token) or self.meta.has_facebook_credentials()
            if not has_creds:
                return {
                    "id": f"demo-meta-{draft.id[:8]}",
                    "post_url": None,
                    "confirmation_badge": "Credentials Missing 🟡",
                    "status": "published_fallback",
                    "mode": "simulated",
                    "error": "FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN not found in .env",
                    "platform": "meta",
                }

            raw_bytes = self._image_bytes_for(draft, image_base64)
            if raw_bytes:
                res = self.meta.publish_facebook_page_photo(
                    image_bytes=raw_bytes, caption=message, page_id=page_id, access_token=access_token
                )
            else:
                res = self.meta.publish_facebook_page_feed(message=message, page_id=page_id, access_token=access_token)

            if res.get("success"):
                post_id = str(res.get("post_id") or res.get("id"))
                return {
                    "id": post_id,
                    "external_post_id": post_id,
                    "post_url": res.get("post_url") or f"https://www.facebook.com/{post_id}",
                    "confirmation_badge": res.get("confirmation_badge", "Published to Mai boovoo 🟢"),
                    "status": "published_live",
                    "mode": "live_facebook",
                    "platform": "meta",
                    "photo_id": res.get("photo_id"),
                }

            mode = res.get("mode", "api_error")
            return {
                "failed": True,
                "id": None,
                "post_url": None,
                "confirmation_badge": res.get("confirmation_badge")
                or ("Token Expired 🟡" if mode == "token_expired" else "Gateway Fallback 🟡"),
                "status": "token_expired" if mode == "token_expired" else "failed",
                "mode": mode,
                "error": res.get("error") or "Facebook publish failed",
                "graph_error": res.get("graph_error"),
                "platform": "meta",
            }

        # Instagram
        ig_user_id = os.getenv("META_IG_USER_ID", "")
        if not isolated and self.meta.access_token and ig_user_id and draft.media_links and draft.media_links[0].startswith("http"):
            try:
                container = self.meta.create_instagram_media_container(ig_user_id, draft.media_links[0], draft.body)
                creation_id = container.get("id")
                if creation_id:
                    pub = self.meta.publish_instagram_media(ig_user_id, creation_id)
                    ig_id = str(pub.get("id") or creation_id)
                    return {
                        "id": ig_id,
                        "external_post_id": ig_id,
                        "post_url": f"https://www.instagram.com/p/{ig_id}",
                        "confirmation_badge": "Published to Instagram 🟢",
                        "status": "published_live",
                        "mode": "live_instagram",
                        "platform": "instagram",
                    }
                return {
                    "failed": True,
                    "error": "Instagram media container was not created",
                    "confirmation_badge": "Gateway Fallback 🟡",
                    "mode": "api_error",
                    "platform": "instagram",
                }
            except Exception as exc:  # noqa: BLE001
                logger.error("Instagram publish failed: %s", type(exc).__name__)
                return {
                    "failed": True,
                    "error": f"Instagram API exception: {type(exc).__name__}: {exc}",
                    "confirmation_badge": "Gateway Fallback 🟡",
                    "mode": "api_error",
                    "platform": "instagram",
                }

        return {
            "id": f"demo-meta-{draft.id[:8]}",
            "mode": "simulated",
            "status": "published_simulated",
            "platform": draft.platform.value,
        }

    @staticmethod
    def _publish_placeholder(draft: ContentDraft, message: str) -> Dict:
        return {
            "id": f"demo-{draft.platform.value}-{draft.id[:8]}",
            "mode": "simulated",
            "status": "published_simulated",
            "platform": draft.platform.value,
            "message": message,
        }

    async def _publish_rpa_placeholder(self, draft: ContentDraft) -> Dict:
        return {
            "id": f"demo-rpa-{draft.platform.value}-{draft.id[:8]}",
            "mode": "simulated",
            "status": "published_simulated",
            "platform": draft.platform.value,
            "adapter": "simulated_rpa_executor",
        }
