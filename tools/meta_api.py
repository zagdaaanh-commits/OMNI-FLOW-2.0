from __future__ import annotations

import os
import logging
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv

logger = logging.getLogger("multi-agent-marketing.meta")


class MetaAPIClient:
    """Graph API adapter for Meta/Facebook/Instagram.

    Tokens and page IDs are loaded dynamically from environment variables (.env).
    """

    _cached_page_tokens: Dict[str, str] = {}

    def __init__(self) -> None:
        self._refresh_credentials()

    def _refresh_credentials(self) -> None:
        load_dotenv(override=True)
        self.page_id = os.getenv("FACEBOOK_PAGE_ID") or os.getenv("META_PAGE_ID", "")
        raw_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN") or os.getenv("META_ACCESS_TOKEN", "")
        self.access_token = "" if (raw_token and "mock" in raw_token.lower()) else raw_token
        raw_user = os.getenv("FACEBOOK_USER_ACCESS_TOKEN", "")
        self.user_token = "" if (raw_user and "mock" in raw_user.lower()) else raw_user
        self.graph_version = os.getenv("META_GRAPH_VERSION", "v19.0")
        self.base_url = f"https://graph.facebook.com/{self.graph_version}"
        self.timeout = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))


    def has_facebook_credentials(self) -> bool:
        self._refresh_credentials()
        return bool(self.page_id and (self.access_token or self.user_token))

    def resolve_page_token(self, page_id: Optional[str] = None) -> Optional[str]:
        """Resolves the valid Page Access Token, checking cache or /me/accounts if a User Token was provided."""
        self._refresh_credentials()
        target_page = str(page_id or self.page_id)
        if target_page in self._cached_page_tokens:
            return self._cached_page_tokens[target_page]

        # If a direct Page Access Token is configured, cache and return it directly
        if self.access_token:
            self._cached_page_tokens[target_page] = self.access_token
            return self.access_token

        # Check if a user_token can query /me/accounts to discover page token
        token_to_try = self.user_token
        if not token_to_try:
            return None

        # Check if the token can query /me/accounts to discover page token
        try:
            resp = requests.get(
                f"https://graph.facebook.com/{self.graph_version}/me/accounts",
                params={"access_token": token_to_try},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                for acc in data:
                    acc_id = str(acc.get("id"))
                    acc_token = acc.get("access_token")
                    if acc_token:
                        self._cached_page_tokens[acc_id] = acc_token
                    if acc_id == target_page and acc_token:
                        return acc_token
                # If target_page wasn't matched but accounts exist, pick the first
                if data and data[0].get("access_token"):
                    self._cached_page_tokens[str(data[0]["id"])] = data[0]["access_token"]
                    if not target_page:
                        return data[0]["access_token"]
        except Exception as e:
            logger.debug(f"Failed to query /me/accounts: {e}")

        # Default to current access token
        return self.access_token

    def test_connection(self) -> Dict[str, Any]:
        """Tests Facebook Graph API credentials and returns live page connection details."""
        self._refresh_credentials()
        if not self.has_facebook_credentials():
            return {
                "connected": False,
                "error": "FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN not configured in .env",
                "page_id": self.page_id,
            }

        resolved_token = self.resolve_page_token(self.page_id) or self.access_token
        if not resolved_token:
            return {
                "connected": False,
                "error": "Facebook Page Access Token not configured. Please paste your token.",
                "page_id": self.page_id,
                "token_expired": True,
            }

        try:
            resp = requests.get(
                f"https://graph.facebook.com/{self.graph_version}/{self.page_id}",
                params={"fields": "id,name,link", "access_token": resolved_token},
                timeout=self.timeout,
            )
            data = resp.json() if resp.content else {}
            if resp.status_code == 200 and "id" in data:
                return {
                    "connected": True,
                    "page_id": data.get("id"),
                    "page_name": data.get("name"),
                    "page_link": data.get("link", f"https://www.facebook.com/{data.get('id')}"),
                    "status_code": 200,
                    "message": "Facebook Page verified & active 🟢",
                }
            else:
                err_msg = data.get("error", {}).get("message", f"HTTP {resp.status_code}")
                err_code = data.get("error", {}).get("code")
                is_expired = (
                    err_code in (190, 200)
                    or "expired" in err_msg.lower()
                    or "validating access token" in err_msg.lower()
                    or "malformed" in err_msg.lower()
                    or "app id" in err_msg.lower()
                )
                return {
                    "connected": False,
                    "error": "Session token expired. Please refresh your Facebook Page Access Token." if is_expired else err_msg,
                    "status_code": resp.status_code,
                    "page_id": self.page_id,
                    "token_expired": is_expired,
                }

        except Exception as exc:
            return {
                "connected": False,
                "error": str(exc),
                "page_id": self.page_id,
                "token_expired": False,
            }

    def publish_facebook_page_feed(
        self,
        message: str,
        page_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes a real HTTP POST to https://graph.facebook.com/v19.0/{page_id}/feed
        with params message=content and access_token=resolved_page_token.
        """
        self._refresh_credentials()
        target_page = page_id or self.page_id
        target_token = access_token or self.resolve_page_token(target_page) or self.access_token

        if not target_page or not target_token:
            return {
                "success": False,
                "error": "FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN missing in .env",
                "mode": "missing_credentials",
                "confirmation_badge": "Credentials Missing 🟡",
            }

        url = f"https://graph.facebook.com/{self.graph_version}/{target_page}/feed"
        try:
            response = requests.post(
                url,
                params={
                    "message": message,
                    "access_token": target_token,
                },
                timeout=self.timeout,
            )
            data = response.json() if response.content else {}

            # Handle case where user token was passed and rejected -> attempt auto-resolution
            if response.status_code != 200 and data.get("error", {}).get("code") in (190, 200):
                resolved = self.resolve_page_token(target_page)
                if resolved and resolved != target_token:
                    target_token = resolved
                    response = requests.post(
                        url,
                        params={
                            "message": message,
                            "access_token": target_token,
                        },
                        timeout=self.timeout,
                    )
                    data = response.json() if response.content else {}

            if response.status_code == 200 and "id" in data:
                fb_id = str(data["id"])
                return {
                    "success": True,
                    "id": fb_id,
                    "post_url": f"https://www.facebook.com/{fb_id}",
                    "confirmation_badge": "Published to Mai boovoo 🟢",
                    "status_code": 200,
                    "message": "Published to Mai boovoo 🟢",
                    "raw": data,
                }
            else:
                err_msg = data.get("error", {}).get("message", f"HTTP {response.status_code}: {response.text}")
                err_code = data.get("error", {}).get("code")
                is_expired = err_code == 190 or "expired" in err_msg.lower() or "validating access token" in err_msg.lower()
                logger.warning(f"Meta Graph API error response: {err_msg}")
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "error": "Facebook Access Token expired. Please refresh your Page token." if is_expired else f"Meta Graph API ({response.status_code}): {err_msg}",
                    "post_url": None,
                    "raw": data,
                    "mode": "token_expired" if is_expired else "api_error",
                    "confirmation_badge": "Token Expired 🟡" if is_expired else "Gateway Fallback 🟡",
                }
        except Exception as exc:
            logger.warning(f"Meta Graph API connection exception: {exc}")
            return {
                "success": False,
                "error": f"Connection exception: {exc}",
                "post_url": f"https://www.facebook.com/{target_page}",
                "mode": "network_exception",
            }

    def publish_facebook_page_photo(
        self,
        image_bytes: bytes,
        caption: str,
        page_id: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Executes a real HTTP POST to https://graph.facebook.com/v19.0/{page_id}/photos
        with files={'source': image_bytes}, params={'access_token': target_token}, and data={'caption': caption}.
        """
        self._refresh_credentials()
        target_page = page_id or self.page_id
        target_token = access_token or self.resolve_page_token(target_page) or self.access_token

        if not target_page or not target_token:
            return {
                "success": False,
                "error": "FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN missing in .env",
                "mode": "missing_credentials",
                "confirmation_badge": "Credentials Missing 🟡",
            }

        url = f"https://graph.facebook.com/{self.graph_version}/{target_page}/photos"
        
        # Detect image format
        mime_type = "image/jpeg"
        filename = "upload.jpg"
        if image_bytes.startswith(b"\x89PNG"):
            mime_type = "image/png"
            filename = "upload.png"
        elif image_bytes.startswith(b"GIF8"):
            mime_type = "image/gif"
            filename = "upload.gif"
        elif image_bytes.startswith(b"RIFF") and len(image_bytes) > 12 and b"WEBP" in image_bytes[:16]:
            mime_type = "image/webp"
            filename = "upload.webp"

        files = {"source": (filename, image_bytes, mime_type)}
        params = {"access_token": target_token}
        data = {"caption": caption}

        try:
            response = requests.post(
                url,
                params=params,
                files=files,
                data=data,
                timeout=self.timeout,
            )
            resp_data = response.json() if response.content else {}

            # Handle token resolution retry if needed
            if response.status_code != 200 and resp_data.get("error", {}).get("code") in (190, 200):
                resolved = self.resolve_page_token(target_page)
                if resolved and resolved != target_token:
                    target_token = resolved
                    params["access_token"] = target_token
                    files = {"source": (filename, image_bytes, mime_type)}
                    response = requests.post(
                        url,
                        params=params,
                        files=files,
                        data=data,
                        timeout=self.timeout,
                    )
                    resp_data = response.json() if response.content else {}

            if response.status_code == 200 and ("id" in resp_data or "post_id" in resp_data):
                post_id = resp_data.get("post_id") or resp_data.get("id")
                photo_id = resp_data.get("id")
                post_url = f"https://www.facebook.com/{post_id}"
                logger.info(f"Successfully published photo to Facebook: {post_url}")
                return {
                    "success": True,
                    "id": str(post_id),
                    "photo_id": str(photo_id),
                    "post_url": post_url,
                    "confirmation_badge": "Published to Mai boovoo 🟢",
                    "status_code": 200,
                    "message": "Published to Mai boovoo 🟢",
                    "raw": resp_data,
                }
            else:
                err_msg = resp_data.get("error", {}).get("message", f"HTTP {response.status_code}: {response.text}")
                err_code = resp_data.get("error", {}).get("code")
                is_expired = (
                    err_code == 190 
                    or "expired" in err_msg.lower() 
                    or "validating access token" in err_msg.lower()
                    or "malformed" in err_msg.lower()
                )
                logger.warning(f"Meta Graph API photo upload error: {err_msg}")
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "error": "Facebook Access Token expired. Please refresh your Page token." if is_expired else f"Meta Graph API ({response.status_code}): {err_msg}",
                    "post_url": None,
                    "raw": resp_data,
                    "mode": "token_expired" if is_expired else "api_error",
                    "confirmation_badge": "Token Expired 🟡" if is_expired else "Gateway Fallback 🟡",
                }
        except Exception as exc:
            logger.warning(f"Meta Graph API photo upload exception: {exc}")
            return {
                "success": False,
                "error": f"Connection exception: {exc}",
                "post_url": f"https://www.facebook.com/{target_page}",
                "mode": "network_exception",
                "confirmation_badge": "Gateway Fallback 🟡",
            }

    def _request(self, method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
        self._refresh_credentials()
        params = kwargs.pop("params", {})
        if "access_token" not in params and self.access_token:
            params["access_token"] = self.access_token
        response = requests.request(
            method,
            f"{self.base_url}/{path.lstrip('/')}",
            params=params,
            timeout=self.timeout,
            **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def publish_page_feed(self, page_id: str, message: str, link: Optional[str] = None) -> Dict[str, Any]:
        data: Dict[str, Any] = {"message": message}
        if link:
            data["link"] = link
        return self._request("POST", f"{page_id}/feed", data=data)

    def create_instagram_media_container(self, ig_user_id: str, image_url: str, caption: str) -> Dict[str, Any]:
        return self._request("POST", f"{ig_user_id}/media", data={"image_url": image_url, "caption": caption})

    def publish_instagram_media(self, ig_user_id: str, creation_id: str) -> Dict[str, Any]:
        return self._request("POST", f"{ig_user_id}/media_publish", data={"creation_id": creation_id})

    def get_page_insights(self, page_id: str, metric: str, period: str = "day") -> Dict[str, Any]:
        return self._request("GET", f"{page_id}/insights", params={"metric": metric, "period": period})

    def get_object(self, object_id: str, fields: str) -> Dict[str, Any]:
        return self._request("GET", object_id, params={"fields": fields})
