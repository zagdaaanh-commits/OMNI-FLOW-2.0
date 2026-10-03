"""File uploads to Supabase Storage, scoped to the caller's workspace.

* ``POST /api/upload/document`` - business license (营业执照) for an agency application.
  PDF / PNG / JPG, max 10 MB, private bucket ``agency-documents``. Returns the storage path
  (pass it to ``POST /api/agency/apply`` as ``business_license_path``) and a 10-minute signed URL.
* ``POST /api/upload/creative`` - campaign image. PNG / JPG / WebP, max 10 MB, public bucket
  ``ad-creatives``. Returns the storage path and the public URL.

Both take ``multipart/form-data`` with a single field named ``file`` and require a signed-in user.
The stored name is always ``<workspace_id>/<uuid>.<ext>``: the client's filename is only kept
(sanitised) for display, and the type comes from the file's bytes, not its name or headers.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import object_storage
from app.config import env_int
from app.redaction import redact
from app.tenancy import TenantContext, require_authenticated

logger = logging.getLogger("omniflow.upload")

router = APIRouter(prefix="/api/upload", tags=["upload"])

SIGNED_URL_TTL_SECONDS = 600
# Multipart framing around the file; anything declaring more than this is refused unread.
_MULTIPART_OVERHEAD = 64 * 1024

_MULTIPART_DOC = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}},
                }
            }
        },
    }
}


async def _read_upload(request: Request) -> Tuple[str, bytes]:
    """The ``file`` field's (original filename, bytes), enforcing the size limit while reading."""
    limit = object_storage.MAX_UPLOAD_BYTES
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit + _MULTIPART_OVERHEAD:
        raise HTTPException(status_code=413, detail="File is too large (max 10 MB)")
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise HTTPException(status_code=400, detail="Send the file as multipart/form-data in a field named 'file'")
    try:
        form = await request.form(max_files=1, max_fields=10)
    except StarletteHTTPException as exc:
        raise HTTPException(status_code=400, detail="Could not read the upload: send one file in a field named 'file'") from exc
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise HTTPException(status_code=400, detail="No file uploaded (form field 'file')")
        data = await upload.read(limit + 1)
        filename = upload.filename or ""
    finally:
        await form.close()
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="File is too large (max 10 MB)")
    if not data:
        raise HTTPException(status_code=400, detail="The file is empty")
    return filename, data


def _check_type(filename: str, data: bytes, allowed: Sequence[str], label: str) -> str:
    file_type = object_storage.detect_file_type(data)
    if file_type not in allowed or not object_storage.extension_matches(filename, file_type):
        raise HTTPException(status_code=415, detail=f"Only {label} files are allowed")
    return file_type


async def _store(
    request: Request, ctx: TenantContext, bucket: str, allowed: Sequence[str], label: str
) -> Tuple[object_storage.ObjectStorage, Dict[str, Any]]:
    if not object_storage.is_configured():
        raise HTTPException(status_code=503, detail="File storage is not configured on this server")

    # Validate before touching the storage client, so bad files are refused immediately.
    filename, data = await _read_upload(request)
    file_type = _check_type(filename, data, allowed, label)
    try:
        storage = await run_in_threadpool(object_storage.get_object_storage)
    except object_storage.StorageUnavailable:
        raise HTTPException(status_code=503, detail="File storage is not configured on this server")
    extension, content_type = object_storage.FILE_TYPES[file_type]
    display_name = object_storage.safe_filename(filename, default=f"upload.{extension}")
    path = object_storage.object_path(ctx.tenant_id, extension)

    metadata = {"original_name": display_name, "uploaded_by": ctx.user_id or ""}
    try:
        await run_in_threadpool(storage.upload, bucket, path, data, content_type, metadata)
    except object_storage.StorageError as exc:
        logger.error("Upload to %s failed for tenant=%s: %s", bucket, ctx.tenant_id, redact(str(exc)))
        raise HTTPException(status_code=502, detail="Could not store the file. Please try again.")
    logger.info("Stored %s/%s (%d bytes) for tenant=%s", bucket, path, len(data), ctx.tenant_id)
    return storage, {
        "bucket": bucket,
        "path": path,
        "filename": display_name,
        "content_type": content_type,
        "size": len(data),
    }


@router.post("/document", openapi_extra=_MULTIPART_DOC)
async def upload_document(request: Request, ctx: TenantContext = Depends(require_authenticated)) -> Dict[str, Any]:
    """Business license (营业执照) for an agency application: PDF, PNG or JPG up to 10 MB."""
    storage, result = await _store(
        request, ctx, object_storage.AGENCY_DOCUMENTS_BUCKET, object_storage.DOCUMENT_TYPES, "PDF, PNG or JPG"
    )
    ttl = env_int("DOCUMENT_SIGNED_URL_TTL_SECONDS", SIGNED_URL_TTL_SECONDS)
    try:
        result["signed_url"] = await run_in_threadpool(storage.signed_url, result["bucket"], result["path"], ttl)
        result["expires_in"] = ttl
    except object_storage.StorageError as exc:
        # The file is stored; only the preview link is missing.
        logger.warning("Could not sign %s for tenant=%s: %s", result["path"], ctx.tenant_id, redact(str(exc)))
        result["signed_url"] = None
        result["expires_in"] = None
    return result


@router.post("/creative", openapi_extra=_MULTIPART_DOC)
async def upload_creative(request: Request, ctx: TenantContext = Depends(require_authenticated)) -> Dict[str, Any]:
    """Campaign image for publishing: PNG, JPG or WebP up to 10 MB, served from a public URL."""
    storage, result = await _store(
        request, ctx, object_storage.AD_CREATIVES_BUCKET, object_storage.CREATIVE_TYPES, "PNG, JPG or WebP"
    )
    result["public_url"] = storage.public_url(result["bucket"], result["path"])
    return result
