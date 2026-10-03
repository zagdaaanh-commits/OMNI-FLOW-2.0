"""Supabase Storage for workspace files, through the official supabase-py client.

Buckets (created by ``scripts/supabase_storage_setup.sql``):

* ``agency-documents`` - private. Business licenses (营业执照) attached to agency applications;
  read only through short-lived signed URLs.
* ``ad-creatives`` - public. Campaign images that Meta / social platforms fetch by URL.

Every object is stored at ``<workspace_id>/<uuid>.<ext>``. The API talks to Storage with the
service-role key, which bypasses Row Level Security, so the path is always built here from the
caller's verified workspace and never from client input; the ``storage.objects`` policies are
the second line of defence for any client that talks to Storage directly with a Supabase JWT.

Configuration: ``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``. Without them uploads answer
503 and the UI hides the upload field. HTTP goes through :func:`tools.http_client.build_httpx_client`,
so ``OUTBOUND_PROXY_URL`` applies here as it does for Meta.
"""
from __future__ import annotations

import logging
import re
import threading
import unicodedata
from pathlib import PureWindowsPath
from typing import Any, Dict, Optional, Tuple
from uuid import uuid4

from app.config import env_float, env_str
from app.tenancy import is_valid_tenant_id
from tools.http_client import build_httpx_client

logger = logging.getLogger("omniflow.object_storage")

AGENCY_DOCUMENTS_BUCKET = "agency-documents"
AD_CREATIVES_BUCKET = "ad-creatives"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Detected type -> (stored extension, content type). The type always comes from the file's own
# bytes; the client's filename and Content-Type header are never trusted.
FILE_TYPES: Dict[str, Tuple[str, str]] = {
    "pdf": ("pdf", "application/pdf"),
    "png": ("png", "image/png"),
    "jpeg": ("jpg", "image/jpeg"),
    "webp": ("webp", "image/webp"),
}
DOCUMENT_TYPES = ("pdf", "png", "jpeg")
CREATIVE_TYPES = ("png", "jpeg", "webp")
# Filename extensions accepted for each detected type.
_EXTENSIONS = {"pdf": {"pdf"}, "png": {"png"}, "jpeg": {"jpg", "jpeg"}, "webp": {"webp"}}

# '<workspace>/<uuid>.<ext>' exactly as built by object_path().
_OBJECT_PATH_RE = re.compile(
    r"^(?P<workspace>[A-Za-z0-9][A-Za-z0-9_\-]{0,63})/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.(?P<ext>pdf|png|jpg|webp)$"
)


class StorageUnavailable(RuntimeError):
    """File storage is not configured on this server."""


class StorageError(RuntimeError):
    """Storage rejected the request or could not be reached (message is safe to log)."""


# ---------------------------------------------------------------------------- file checks
def detect_file_type(data: bytes) -> Optional[str]:
    """Identify PDF / PNG / JPEG / WebP from the leading bytes ("magic numbers")."""
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def extension_matches(filename: str, file_type: str) -> bool:
    """A filename extension, when there is one, must agree with the detected type."""
    suffix = PureWindowsPath(filename or "").suffix.lower().lstrip(".")
    return not suffix or suffix in _EXTENSIONS.get(file_type, set())


def safe_filename(name: str, default: str = "file") -> str:
    """Display name for an upload: no directories, control or reserved characters, max 120 chars.

    Used only for display and object metadata; storage paths never contain client-supplied names.
    """
    name = PureWindowsPath(name or "").name  # strips both '/' and '\\' directory parts
    name = unicodedata.normalize("NFKC", name)
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"/\\|?*')
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return default
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 119 - len(ext)] + "." + ext) if dot and len(ext) <= 10 else name[:120]
    return name


def object_path(workspace_id: str, extension: str) -> str:
    if not is_valid_tenant_id(workspace_id):
        raise ValueError("invalid workspace id")
    return f"{workspace_id}/{uuid4()}.{extension}"


def parse_object_path(path: str) -> Optional[Tuple[str, str]]:
    """(workspace_id, extension) for a path built by :func:`object_path`, else None."""
    match = _OBJECT_PATH_RE.match(path or "")
    return (match.group("workspace"), match.group("ext")) if match else None


# ------------------------------------------------------------------------------- client
class ObjectStorage:
    """Thin wrapper over a supabase-py ``Client`` (sync; call it from a worker thread)."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def upload(self, bucket: str, path: str, data: bytes, content_type: str, metadata: Optional[Dict[str, str]] = None) -> None:
        options: Dict[str, Any] = {"content-type": content_type, "upsert": "false", "cache-control": "3600"}
        if metadata:
            options["metadata"] = metadata
        self._call("upload", lambda: self._client.storage.from_(bucket).upload(path, data, options))

    def signed_url(self, bucket: str, path: str, expires_in: int) -> str:
        result = self._call("sign", lambda: self._client.storage.from_(bucket).create_signed_url(path, expires_in))
        url = (result or {}).get("signedURL") or (result or {}).get("signedUrl")
        if not url:
            raise StorageError("Storage returned no signed URL")
        return url

    def public_url(self, bucket: str, path: str) -> str:
        return str(self._client.storage.from_(bucket).get_public_url(path)).rstrip("?")

    @staticmethod
    def _call(action: str, fn: Any) -> Any:
        import httpx
        from storage3.exceptions import StorageApiError

        try:
            return fn()
        except StorageApiError as exc:
            status = getattr(exc, "status", "?")
            raise StorageError(f"Storage {action} failed (HTTP {status}): {getattr(exc, 'message', '')}") from exc
        except httpx.HTTPError as exc:
            raise StorageError(f"Storage {action} failed: {type(exc).__name__}") from exc


def storage_settings() -> Tuple[str, str]:
    return env_str("SUPABASE_URL").rstrip("/"), env_str("SUPABASE_SERVICE_ROLE_KEY")


def is_configured() -> bool:
    url, key = storage_settings()
    return url.lower().startswith(("https://", "http://")) and bool(key)


_lock = threading.Lock()
_cached: Optional[Tuple[Tuple[str, str], ObjectStorage]] = None


def get_object_storage() -> ObjectStorage:
    """The shared client for the configured project; raises :class:`StorageUnavailable`."""
    global _cached
    if not is_configured():
        raise StorageUnavailable("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are not set")
    settings = storage_settings()
    with _lock:
        if _cached is None or _cached[0] != settings:
            try:
                import supabase  # noqa: F401
            except ImportError as exc:  # pragma: no cover - dependency listed in requirements.txt
                logger.error("supabase-py is not installed; file uploads are disabled")
                raise StorageUnavailable("supabase-py is not installed") from exc
            from supabase import create_client
            from supabase.lib.client_options import SyncClientOptions

            options = SyncClientOptions(
                auto_refresh_token=False,
                persist_session=False,
                httpx_client=build_httpx_client(timeout=env_float("STORAGE_TIMEOUT_SECONDS", 30.0)),
            )
            _cached = (settings, ObjectStorage(create_client(settings[0], settings[1], options=options)))
        return _cached[1]


def reset_object_storage() -> None:
    global _cached
    with _lock:
        _cached = None
