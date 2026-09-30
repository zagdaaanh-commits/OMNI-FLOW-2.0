"""PostgreSQL / Supabase store (production).

Isolation model (defence in depth)
----------------------------------
1. Every tenant-scoped statement filters ``tenant_id`` explicitly in its ``WHERE``.
2. Every transaction first runs ``set_config('app.tenant_id', <tenant>, true)`` so the
   Row Level Security policies from ``scripts/init_supabase.sql`` also apply when the
   app connects as a non-owner role.  Cross-tenant worker operations set
   ``app.bypass_rls = 'on'`` (honoured only by policies, never by clients).
3. Task claiming uses ``UPDATE ... WHERE id = %s AND status = %s`` (compare-and-swap),
   so any number of workers can poll concurrently without double-publishing.

The connection string comes from ``DATABASE_URL`` (Supabase: append ``?sslmode=require``).
Use the Supabase *pooler* (transaction mode) URL for many web workers; prepared
statements are disabled for compatibility with PgBouncer.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.config import env_int, is_production
from db.base import DEFAULT_TENANT_ID, CrossTenantWriteError, mask_token, slugify, utcnow
from db.passwords import hash_password, needs_rehash, verify_password
from models.schemas import Campaign, ContentDraft, PublishStatus, PublishTask

logger = logging.getLogger("omniflow.db.postgres")

_ACCOUNT_COLUMNS = (
    "id, tenant_id, user_id, platform, account_id, account_name, access_token, status, permissions, created_at, updated_at"
)


def _json_safe(value: Any) -> Any:
    """Datetimes -> ISO strings so JSONB payloads round-trip through pydantic."""
    return json.loads(json.dumps(value, default=str))


class PostgresStore:
    def __init__(self, dsn: str, *, min_size: Optional[int] = None, max_size: Optional[int] = None) -> None:
        self.dsn = dsn
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size if min_size is not None else env_int("DB_POOL_MIN", 1),
            max_size=max_size if max_size is not None else env_int("DB_POOL_MAX", 10),
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
            timeout=env_int("DB_POOL_TIMEOUT_SECONDS", 30),
            name="omniflow",
        )
        self.pool.wait(timeout=30)
        self.ensure_tenant(DEFAULT_TENANT_ID, "Default Workspace")
        self._seed_default_user()

    # ------------------------------------------------------------ connection
    @contextmanager
    def _tx(self, tenant_id: Optional[str] = None) -> Iterator[psycopg.Connection]:
        """One transaction with RLS context. ``tenant_id=None`` => worker/global scope."""
        with self.pool.connection() as conn:
            with conn.transaction():
                if tenant_id is None:
                    conn.execute("SELECT set_config('app.bypass_rls', 'on', true)")
                else:
                    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
                yield conn

    def close(self) -> None:
        self.pool.close()

    # --------------------------------------------------------------- tenants
    def create_tenant(self, name: str, slug: Optional[str] = None) -> Dict[str, Any]:
        tenant_id = str(uuid4())
        base_slug = slugify(slug or name)
        with self._tx(None) as conn:
            candidate, suffix = base_slug, 1
            while conn.execute("SELECT 1 FROM tenants WHERE slug = %s", (candidate,)).fetchone():
                suffix += 1
                candidate = f"{base_slug}-{suffix}"
            conn.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%s, %s, %s)", (tenant_id, name.strip() or "Workspace", candidate)
            )
        return self.get_tenant(tenant_id)  # type: ignore[return-value]

    def ensure_tenant(self, tenant_id: str, name: Optional[str] = None) -> Dict[str, Any]:
        with self._tx(None) as conn:
            conn.execute(
                "INSERT INTO tenants (id, name, slug) VALUES (%s, %s, NULL) ON CONFLICT (id) DO NOTHING",
                (tenant_id, name or tenant_id),
            )
        return self.get_tenant(tenant_id)  # type: ignore[return-value]

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self._tx(tenant_id) as conn:
            return _stringify(conn.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,)).fetchone())

    # ----------------------------------------------------------------- users
    def _seed_default_user(self) -> None:
        if is_production():
            return
        with self._tx(None) as conn:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE tenant_id = %s OR lower(email) = 'admin@omniflow.ai' LIMIT 1", (DEFAULT_TENANT_ID,)
            ).fetchone()
            if not exists:
                conn.execute(
                    """
                    INSERT INTO users (id, tenant_id, email, full_name, password_hash, role, company, avatar_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (str(uuid4()), DEFAULT_TENANT_ID, "admin@omniflow.ai", "Alex Rivera", hash_password("admin123"),
                     "Brand Director", "Global Brand HQ", ""),
                )

    def create_user(
        self,
        email: str,
        full_name: str,
        password: str,
        role: str = "Brand Lead",
        company: str = "Global Brand HQ",
        avatar_url: str = "",
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Dict[str, Any]:
        uid = str(uuid4())
        with self._tx(tenant_id) as conn:
            conn.execute(
                """
                INSERT INTO users (id, tenant_id, email, full_name, password_hash, role, company, avatar_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (uid, tenant_id, email.strip().lower(), full_name.strip(), hash_password(password), role, company, avatar_url),
            )
        return self.get_user_by_id(uid, tenant_id=tenant_id)  # type: ignore[return-value]

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        with self._tx(None) as conn:
            return _stringify(
                conn.execute("SELECT * FROM users WHERE lower(email) = %s", (email.strip().lower(),)).fetchone()
            )

    def get_user_by_id(self, user_id: str, *, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._tx(tenant_id) as conn:
            if tenant_id is None:
                row = conn.execute("SELECT * FROM users WHERE id = %s", (user_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM users WHERE id = %s AND tenant_id = %s", (user_id, tenant_id)).fetchone()
        data = _stringify(row)
        if data:
            data.pop("password_hash", None)
        return data

    def list_users(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[Dict[str, Any]]:
        with self._tx(tenant_id) as conn:
            rows = conn.execute(
                "SELECT id, email, full_name, role, company, avatar_url, created_at, tenant_id FROM users "
                "WHERE tenant_id = %s ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
        return [_stringify(r) for r in rows]  # type: ignore[misc]

    def authenticate_user(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        user = self.get_user_by_email(email)
        if not user or not verify_password(password, user.get("password_hash", "")):
            return None
        if needs_rehash(user["password_hash"]):
            with self._tx(user["tenant_id"]) as conn:
                conn.execute(
                    "UPDATE users SET password_hash = %s WHERE id = %s AND tenant_id = %s",
                    (hash_password(password), user["id"], user["tenant_id"]),
                )
        user.pop("password_hash", None)
        return user

    # ------------------------------------------------------ social accounts
    @staticmethod
    def _account(row: Optional[dict]) -> Optional[Dict[str, Any]]:
        data = _stringify(row)
        if not data:
            return None
        data["masked_token"] = mask_token(data.get("access_token"))
        return data

    def save_connected_account(
        self,
        user_id: str,
        platform: str,
        account_id: str,
        account_name: str,
        access_token: str,
        status: str = "connected",
        permissions: Optional[List[str]] = None,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Dict[str, Any]:
        platform = platform.lower()
        perms = permissions or ["publish_posts", "read_insights"]
        with self._tx(tenant_id) as conn:
            row = conn.execute(
                f"""
                INSERT INTO social_accounts
                    (id, tenant_id, user_id, platform, account_id, account_name, access_token, status, permissions)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, platform, account_id) DO UPDATE SET
                    user_id = EXCLUDED.user_id, account_name = EXCLUDED.account_name,
                    access_token = EXCLUDED.access_token, status = EXCLUDED.status,
                    permissions = EXCLUDED.permissions
                RETURNING {_ACCOUNT_COLUMNS}
                """,
                (str(uuid4()), tenant_id, user_id, platform, account_id, account_name, access_token, status, Jsonb(perms)),
            ).fetchone()
        return self._account(row)  # type: ignore[return-value]

    def get_connected_account(
        self, user_id: Optional[str], platform: str, *, tenant_id: str = DEFAULT_TENANT_ID, account_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        sql = f"SELECT {_ACCOUNT_COLUMNS} FROM social_accounts WHERE tenant_id = %s AND platform = %s"
        params: List[Any] = [tenant_id, platform.lower()]
        if user_id is not None:  # None => any user within the tenant (used by the scheduler)
            sql += " AND (user_id = %s OR user_id = 'global')"
            params.append(user_id)
        if account_id is not None:
            sql += " AND account_id = %s"
            params.append(account_id)
        sql += " ORDER BY updated_at DESC LIMIT 1"
        with self._tx(tenant_id) as conn:
            return self._account(conn.execute(sql, params).fetchone())

    def list_connected_accounts(
        self, user_id: Optional[str] = "global", *, tenant_id: str = DEFAULT_TENANT_ID
    ) -> List[Dict[str, Any]]:
        """``user_id=None`` lists every account of the tenant."""
        sql = f"SELECT {_ACCOUNT_COLUMNS} FROM social_accounts WHERE tenant_id = %s"
        params: List[Any] = [tenant_id]
        if user_id is not None:
            sql += " AND (user_id = %s OR user_id = 'global')"
            params.append(user_id)
        with self._tx(tenant_id) as conn:
            rows = conn.execute(sql + " ORDER BY updated_at DESC", params).fetchall()
        return [self._account(r) for r in rows]  # type: ignore[misc]

    def delete_connected_account(self, account_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
        with self._tx(tenant_id) as conn:
            cur = conn.execute("DELETE FROM social_accounts WHERE id = %s AND tenant_id = %s", (account_id, tenant_id))
            return cur.rowcount > 0

    def delete_connected_accounts_for_platform(self, platform: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> int:
        with self._tx(tenant_id) as conn:
            cur = conn.execute(
                "DELETE FROM social_accounts WHERE tenant_id = %s AND platform = %s", (tenant_id, platform.lower())
            )
            return cur.rowcount

    # ------------------------------------------------ campaigns/drafts/tasks
    @staticmethod
    def _payload(item: Any) -> Jsonb:
        return Jsonb(_json_safe(item.model_dump(mode="json")))

    @staticmethod
    def _guard(cur: Any, table: str, item_id: str, tenant_id: str) -> None:
        if cur.rowcount == 0:
            raise CrossTenantWriteError(f"{table} row {item_id} belongs to another tenant (writer tenant={tenant_id})")

    def save_campaign(self, item: Campaign) -> Campaign:
        item.updated_at = utcnow()
        with self._tx(item.tenant_id) as conn:
            cur = conn.execute(
                """
                INSERT INTO campaigns (id, tenant_id, name, status, payload, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, status = EXCLUDED.status,
                    payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
                WHERE campaigns.tenant_id = EXCLUDED.tenant_id
                """,
                (item.id, item.tenant_id, item.name, item.status.value, self._payload(item), item.created_at, item.updated_at),
            )
            self._guard(cur, "campaigns", item.id, item.tenant_id)
        return item

    def save_draft(self, item: ContentDraft) -> ContentDraft:
        with self._tx(item.tenant_id) as conn:
            cur = conn.execute(
                """
                INSERT INTO content_drafts (id, tenant_id, campaign_id, platform, language, payload, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET campaign_id = EXCLUDED.campaign_id, platform = EXCLUDED.platform,
                    language = EXCLUDED.language, payload = EXCLUDED.payload
                WHERE content_drafts.tenant_id = EXCLUDED.tenant_id
                """,
                (item.id, item.tenant_id, item.campaign_id, item.platform.value, item.language, self._payload(item), item.created_at),
            )
            self._guard(cur, "content_drafts", item.id, item.tenant_id)
        return item

    def save_task(self, item: PublishTask) -> PublishTask:
        item.updated_at = utcnow()
        with self._tx(item.tenant_id) as conn:
            cur = conn.execute(
                """
                INSERT INTO scheduled_tasks
                    (id, tenant_id, campaign_id, content_draft_id, platform, status, scheduled_at, published_at,
                     external_post_id, attempts, error, payload, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET campaign_id = EXCLUDED.campaign_id,
                    content_draft_id = EXCLUDED.content_draft_id, platform = EXCLUDED.platform,
                    status = EXCLUDED.status, scheduled_at = EXCLUDED.scheduled_at,
                    published_at = EXCLUDED.published_at, external_post_id = EXCLUDED.external_post_id,
                    attempts = EXCLUDED.attempts, error = EXCLUDED.error, payload = EXCLUDED.payload,
                    updated_at = EXCLUDED.updated_at
                WHERE scheduled_tasks.tenant_id = EXCLUDED.tenant_id
                """,
                (
                    item.id, item.tenant_id, item.campaign_id, item.content_draft_id, item.platform.value,
                    item.status.value, item.scheduled_at, item.published_at, item.external_post_id,
                    item.attempts, item.error, self._payload(item), item.updated_at,
                ),
            )
            self._guard(cur, "scheduled_tasks", item.id, item.tenant_id)
        return item

    # payload JSONB is authoritative for the model; columns drive queries.
    def _get(self, table: str, model: Any, item_id: str, tenant_id: Optional[str]) -> Any:
        with self._tx(tenant_id) as conn:
            if tenant_id is None:
                row = conn.execute(f"SELECT payload, tenant_id FROM {table} WHERE id = %s", (item_id,)).fetchone()
            else:
                row = conn.execute(
                    f"SELECT payload, tenant_id FROM {table} WHERE id = %s AND tenant_id = %s", (item_id, tenant_id)
                ).fetchone()
        if not row:
            return None
        data = dict(row["payload"])
        data["tenant_id"] = row["tenant_id"]
        return model.model_validate(data)

    def _list(self, table: str, model: Any, tenant_id: str, extra: str = "", params: tuple = ()) -> List[Any]:
        with self._tx(tenant_id) as conn:
            rows = conn.execute(
                f"SELECT payload, tenant_id FROM {table} WHERE tenant_id = %s{extra} ORDER BY created_at DESC, id DESC",
                (tenant_id, *params),
            ).fetchall()
        out = []
        for row in rows:
            data = dict(row["payload"])
            data["tenant_id"] = row["tenant_id"]
            out.append(model.model_validate(data))
        return out

    def get_campaign(self, item_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Campaign]:
        return self._get("campaigns", Campaign, item_id, tenant_id)

    def get_draft(self, item_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[ContentDraft]:
        return self._get("content_drafts", ContentDraft, item_id, tenant_id)

    def get_task(self, item_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[PublishTask]:
        return self._get("scheduled_tasks", PublishTask, item_id, tenant_id)

    def list_campaigns(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[Campaign]:
        return self._list("campaigns", Campaign, tenant_id)

    def list_drafts(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[ContentDraft]:
        return self._list("content_drafts", ContentDraft, tenant_id)

    def list_tasks(self, *, tenant_id: str = DEFAULT_TENANT_ID, status: Optional[str] = None) -> List[PublishTask]:
        if status:
            return self._list("scheduled_tasks", PublishTask, tenant_id, " AND status = %s", (status,))
        return self._list("scheduled_tasks", PublishTask, tenant_id)

    def counts(self, *, tenant_id: Optional[str] = None) -> Dict[str, int]:
        mapping = {
            "campaigns": "campaigns",
            "drafts": "content_drafts",
            "tasks": "scheduled_tasks",
            "users": "users",
            "connected_accounts": "social_accounts",
        }
        result: Dict[str, int] = {}
        with self._tx(tenant_id) as conn:
            for key, table in mapping.items():
                if tenant_id is None:
                    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                else:
                    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE tenant_id = %s", (tenant_id,)).fetchone()
                result[key] = int(row["n"])  # type: ignore[index]
        return result

    # ------------------------------------------------ scheduler (worker only)
    def get_task_any_tenant(self, item_id: str) -> Optional[PublishTask]:
        return self._get("scheduled_tasks", PublishTask, item_id, None)

    def get_draft_any_tenant(self, item_id: str) -> Optional[ContentDraft]:
        return self._get("content_drafts", ContentDraft, item_id, None)

    def _tasks_where(self, where: str, params: tuple, limit: Optional[int] = None) -> List[PublishTask]:
        sql = f"SELECT payload, tenant_id FROM scheduled_tasks WHERE {where}"
        if limit is not None:
            sql += " LIMIT %s"
            params = (*params, limit)
        with self._tx(None) as conn:
            rows = conn.execute(sql, params).fetchall()
        tasks = []
        for row in rows:
            data = dict(row["payload"])
            data["tenant_id"] = row["tenant_id"]
            tasks.append(PublishTask.model_validate(data))
        return tasks

    def list_due_tasks(self, now: datetime, limit: int = 50) -> List[PublishTask]:
        # Read-only listing; safety against double publishing comes from claim_task's CAS.
        return self._tasks_where(
            "status = %s AND scheduled_at IS NOT NULL AND scheduled_at <= %s ORDER BY scheduled_at",
            (PublishStatus.SCHEDULED.value, now),
            limit,
        )

    def list_scheduled_tasks(self) -> List[PublishTask]:
        return self._tasks_where("status = %s ORDER BY scheduled_at", (PublishStatus.SCHEDULED.value,))

    def list_recoverable_tasks(self, stale_before: datetime) -> List[PublishTask]:
        return self._tasks_where(
            "status = %s AND updated_at < %s", (PublishStatus.PUBLISHING.value, stale_before)
        )

    def claim_task(self, task_id: str, *, expected_status: str, new_status: str) -> bool:
        """Atomic CAS; the JSONB payload is updated in the same statement (no read-modify-write race)."""
        attempts_inc = 1 if new_status == PublishStatus.PUBLISHING.value else 0
        with self._tx(None) as conn:
            cur = conn.execute(
                """
                UPDATE scheduled_tasks
                SET status = %s,
                    attempts = attempts + %s,
                    updated_at = now(),
                    payload = jsonb_set(
                        jsonb_set(
                            jsonb_set(payload, '{status}', to_jsonb(%s::text)),
                            '{attempts}', to_jsonb(attempts + %s)),
                        '{updated_at}', to_jsonb(to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')))
                WHERE id = %s AND status = %s
                RETURNING id
                """,
                (new_status, attempts_inc, new_status, attempts_inc, task_id, expected_status),
            )
            return cur.fetchone() is not None


def _stringify(row: Optional[dict]) -> Optional[Dict[str, Any]]:
    """Convert datetimes to ISO strings so API/Pydantic consumers see the same shape as SQLite."""
    if row is None:
        return None
    out: Dict[str, Any] = {}
    for key, value in row.items():
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out
