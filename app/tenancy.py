"""Tenant resolution for multi-tenant request handling.

Resolution order for :func:`get_tenant_context`:

1. ``Authorization: Bearer <token>`` issued by :func:`issue_access_token` -> the
   token's tenant (an invalid or expired bearer token is always a 401).
2. ``X-Tenant-ID`` header, only when ``ALLOW_TENANT_HEADER`` is truthy (local
   development convenience; never enable in production).
3. ``REQUIRE_AUTH`` truthy -> 401.
4. Otherwise the shared ``default`` tenant (single-merchant / demo mode, which keeps
   the bundled dashboard working without login).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request, status

from app.config import env_bool, env_int, is_production, load_environment
from app.security import InvalidTokenError, sign_payload, verify_payload

DEFAULT_TENANT_ID = "default"
ACCESS_TOKEN_TTL_SECONDS = 7 * 24 * 3600
_TENANT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    user_id: Optional[str] = None
    authenticated: bool = False


def is_valid_tenant_id(value: str) -> bool:
    return bool(value) and bool(_TENANT_ID_RE.match(value))


def issue_access_token(user_id: str, tenant_id: str, ttl_seconds: Optional[int] = None) -> str:
    load_environment()
    ttl = ttl_seconds or env_int("ACCESS_TOKEN_TTL_SECONDS", ACCESS_TOKEN_TTL_SECONDS)
    return sign_payload({"sub": user_id, "tid": tenant_id, "typ": "access"}, ttl)


def decode_access_token(token: str) -> TenantContext:
    claims = verify_payload(token)
    if claims.get("typ") != "access":
        raise InvalidTokenError("Not an access token")
    tenant_id = claims.get("tid")
    if not isinstance(tenant_id, str) or not is_valid_tenant_id(tenant_id):
        raise InvalidTokenError("Token has no valid tenant")
    user_id = claims.get("sub")
    return TenantContext(tenant_id=tenant_id, user_id=str(user_id) if user_id else None, authenticated=True)


def _bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def get_tenant_context(request: Request) -> TenantContext:
    """FastAPI dependency returning the caller's tenant (see module docstring)."""
    load_environment()
    token = _bearer_token(request)
    if token:
        try:
            ctx = decode_access_token(token)
        except InvalidTokenError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid or expired access token: {exc}",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        request.state.tenant = ctx
        return ctx

    if env_bool("ALLOW_TENANT_HEADER", False) and not is_production():
        header_tenant = (request.headers.get("x-tenant-id") or "").strip()
        if header_tenant:
            if not is_valid_tenant_id(header_tenant):
                raise HTTPException(status_code=400, detail="Invalid X-Tenant-ID header")
            ctx = TenantContext(tenant_id=header_tenant)
            request.state.tenant = ctx
            return ctx

    if env_bool("REQUIRE_AUTH", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    ctx = TenantContext(tenant_id=DEFAULT_TENANT_ID)
    request.state.tenant = ctx
    return ctx


def require_authenticated(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
    """Dependency for endpoints that must never run anonymously."""
    if not ctx.authenticated:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return ctx
