"""SQLite store for local development, tests and single-node demos.

Multi-tenant aware (``tenant_id`` on every row) with an idempotent in-place
migration for databases created by the original single-tenant prototype.
Production deployments use :class:`db.postgres_store.PostgresStore`.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional
from uuid import uuid4

from db.base import (
    DEFAULT_TENANT_ID,
    CrossTenantWriteError,
    iso_utc,
    mask_token,
    slugify,
    utcnow,
)
from db.passwords import hash_password, needs_rehash, verify_password
from models.schemas import Campaign, ContentDraft, PublishStatus, PublishTask

logger = logging.getLogger("omniflow.db.sqlite")

AI_USAGE_CYCLE_DAYS = 30

_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    slug TEXT UNIQUE,
    plan TEXT NOT NULL DEFAULT 'free',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    status TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    company TEXT NOT NULL,
    avatar_url TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connected_accounts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    account_id TEXT NOT NULL,
    account_name TEXT NOT NULL,
    access_token TEXT NOT NULL,
    status TEXT NOT NULL,
    permissions TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agency_applications (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT,
    company_name TEXT NOT NULL,
    credit_code TEXT NOT NULL,
    store_url TEXT NOT NULL,
    contact TEXT NOT NULL,
    remarks TEXT,
    business_license_path TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Billing: one subscription per workspace, AI usage per 30-day cycle, upgrade requests from the UI.
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL UNIQUE,
    plan TEXT NOT NULL DEFAULT 'pro',
    billing_cycle TEXT NOT NULL DEFAULT 'monthly',
    status TEXT NOT NULL DEFAULT 'trial',
    current_period_end TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_tracking (
    tenant_id TEXT PRIMARY KEY,
    ai_runs_count INTEGER NOT NULL DEFAULT 0,
    cycle_start TEXT NOT NULL,
    cycle_end TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS upgrade_requests (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT,
    plan TEXT NOT NULL,
    billing_cycle TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- Online payments (Stripe Checkout Sessions); id is the provider's id, so each is applied once.
CREATE TABLE IF NOT EXISTS billing_payments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'stripe',
    plan TEXT NOT NULL,
    billing_cycle TEXT NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'paid',
    reference TEXT,
    period_end TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS page_comments (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    post_id TEXT,
    parent_id TEXT,
    from_id TEXT,
    from_name TEXT,
    message TEXT,
    verb TEXT NOT NULL DEFAULT 'add',
    created_time TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (tenant_id, comment_id)
);
"""

# Columns written by save_page_comment, in insert order.
_COMMENT_FIELDS = ("page_id", "comment_id", "post_id", "parent_id", "from_id", "from_name", "message", "verb")

# Columns added on top of the prototype schema: (table, column, DDL type/default).
_ADDED_COLUMNS = [
    ("campaigns", "tenant_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_TENANT_ID}'"),
    ("drafts", "tenant_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_TENANT_ID}'"),
    ("drafts", "campaign_id", "TEXT"),
    ("tasks", "tenant_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_TENANT_ID}'"),
    ("tasks", "campaign_id", "TEXT"),
    ("tasks", "scheduled_at", "TEXT"),
    ("tasks", "created_at", "TEXT"),
    ("users", "tenant_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_TENANT_ID}'"),
    ("connected_accounts", "tenant_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_TENANT_ID}'"),
    ("connected_accounts", "created_at", "TEXT"),
    ("agency_applications", "business_license_path", "TEXT"),
]

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_campaigns_tenant ON campaigns (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_drafts_tenant ON drafts (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks (tenant_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks (status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id);
CREATE INDEX IF NOT EXISTS idx_accounts_tenant_platform ON connected_accounts (tenant_id, platform, updated_at);
CREATE INDEX IF NOT EXISTS idx_accounts_platform_account ON connected_accounts (platform, account_id);
CREATE INDEX IF NOT EXISTS idx_agency_applications_tenant ON agency_applications (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_page_comments_tenant_post ON page_comments (tenant_id, post_id, created_time);
CREATE INDEX IF NOT EXISTS idx_upgrade_requests_tenant ON upgrade_requests (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_billing_payments_tenant ON billing_payments (tenant_id, created_at);
"""


class SQLiteStore:
    """SQLite persistence with tenant isolation, CAS task claiming and PBKDF2 passwords."""

    def __init__(self, path: str = "./data/marketing.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ----------------------------------------------------------- connections
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # Backwards compatibility for code that used the old helper.
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def close(self) -> None:  # connections are per-operation
        return None

    # -------------------------------------------------------------- migration
    def _init_db(self) -> None:
        with self._conn() as conn:
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except sqlite3.DatabaseError as exc:  # e.g. network filesystems
                logger.warning("Could not enable WAL mode: %s", exc)
            conn.executescript(_BASE_SCHEMA)
            for table, column, ddl in _ADDED_COLUMNS:
                existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    logger.info("Migrated SQLite schema: added %s.%s", table, column)
            conn.executescript(_INDEXES)
            self._backfill(conn)
            now = iso_utc(utcnow())
            conn.execute(
                "INSERT OR IGNORE INTO tenants (id, name, slug, created_at, updated_at) VALUES (?,?,?,?,?)",
                (DEFAULT_TENANT_ID, "Default Workspace", DEFAULT_TENANT_ID, now, now),
            )
            # The default workspace is the operator's house account: Agency VIP without an end date.
            conn.execute(
                "INSERT OR IGNORE INTO subscriptions "
                "(id, tenant_id, plan, billing_cycle, status, current_period_end, created_at, updated_at) "
                "VALUES (?, ?, 'agency', 'annual', 'active', NULL, ?, ?)",
                (str(uuid4()), DEFAULT_TENANT_ID, now, now),
            )

    @staticmethod
    def _backfill(conn: sqlite3.Connection) -> None:
        """Populate normalized task/draft columns for rows written by the prototype."""
        rows = conn.execute(
            "SELECT id, payload FROM tasks WHERE campaign_id IS NULL OR created_at IS NULL"
        ).fetchall()
        for row in rows:
            try:
                task = PublishTask.model_validate(json.loads(row["payload"]))
            except Exception:  # noqa: BLE001 - leave unparseable legacy rows untouched
                continue
            conn.execute(
                "UPDATE tasks SET campaign_id = ?, scheduled_at = ?, created_at = COALESCE(created_at, ?) WHERE id = ?",
                (task.campaign_id, iso_utc(task.scheduled_at), iso_utc(utcnow()), row["id"]),
            )
        rows = conn.execute("SELECT id, payload FROM drafts WHERE campaign_id IS NULL").fetchall()
        for row in rows:
            try:
                campaign_id = json.loads(row["payload"]).get("campaign_id")
            except Exception:  # noqa: BLE001
                continue
            conn.execute("UPDATE drafts SET campaign_id = ? WHERE id = ?", (campaign_id, row["id"]))
        conn.execute("UPDATE connected_accounts SET created_at = updated_at WHERE created_at IS NULL")

    # ------------------------------------------------------------ passwords
    @staticmethod
    def hash_password(password: str) -> str:
        return hash_password(password)

    @staticmethod
    def verify_password(password: str, stored_hash: str) -> bool:
        return verify_password(password, stored_hash)

    # --------------------------------------------------------------- tenants
    def create_tenant(self, name: str, slug: Optional[str] = None) -> Dict[str, Any]:
        tenant_id = str(uuid4())
        base_slug = slugify(slug or name)
        now = iso_utc(utcnow())
        with self._conn() as conn:
            candidate = base_slug
            suffix = 1
            while conn.execute("SELECT 1 FROM tenants WHERE slug = ?", (candidate,)).fetchone():
                suffix += 1
                candidate = f"{base_slug}-{suffix}"
            conn.execute(
                "INSERT INTO tenants (id, name, slug, created_at, updated_at) VALUES (?,?,?,?,?)",
                (tenant_id, name.strip() or "Workspace", candidate, now, now),
            )
        return self.get_tenant(tenant_id)  # type: ignore[return-value]

    def ensure_tenant(self, tenant_id: str, name: Optional[str] = None) -> Dict[str, Any]:
        now = iso_utc(utcnow())
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO tenants (id, name, slug, created_at, updated_at) VALUES (?,?,?,?,?)",
                (tenant_id, name or tenant_id, None, now, now),
            )
        return self.get_tenant(tenant_id)  # type: ignore[return-value]

    def get_tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
            return dict(row) if row else None

    # ----------------------------------------------------------------- users
    def create_user(
        self,
        email: str,
        full_name: str,
        password: str,
        role: str = "Brand Lead",
        company: str = "",
        avatar_url: str = "",
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
    ) -> Dict[str, Any]:
        email = email.strip().lower()
        uid = str(uuid4())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, role, company, avatar_url, created_at, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, email, full_name.strip(), hash_password(password), role, company, avatar_url, iso_utc(utcnow()), tenant_id),
            )
        return self.get_user_by_id(uid)  # type: ignore[return-value]

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Global lookup (emails are unique across tenants); includes ``password_hash``."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE lower(email) = ?", (email.strip().lower(),)).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: str, *, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            if tenant_id is None:
                row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            else:
                row = conn.execute("SELECT * FROM users WHERE id = ? AND tenant_id = ?", (user_id, tenant_id)).fetchone()
            if not row:
                return None
            data = dict(row)
            data.pop("password_hash", None)
            return data

    def list_users(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, email, full_name, role, company, avatar_url, created_at, tenant_id FROM users "
                "WHERE tenant_id = ? ORDER BY created_at",
                (tenant_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def authenticate_user(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        user = self.get_user_by_email(email)
        if not user or not verify_password(password, user.get("password_hash", "")):
            return None
        if needs_rehash(user["password_hash"]):
            with self._conn() as conn:
                conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user["id"]))
        user.pop("password_hash", None)
        return user

    # ------------------------------------------------------ social accounts
    @staticmethod
    def _account_row(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        if data.get("permissions"):
            try:
                data["permissions"] = json.loads(data["permissions"])
            except (TypeError, ValueError):
                pass
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
        now = iso_utc(utcnow())
        perms_json = json.dumps(permissions or ["publish_posts", "read_insights"])
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id FROM connected_accounts WHERE tenant_id = ? AND platform = ? AND account_id = ?",
                (tenant_id, platform, account_id),
            ).fetchone()
            if row:
                acc_id = row["id"]
                conn.execute(
                    """
                    UPDATE connected_accounts SET user_id = ?, account_name = ?, access_token = ?, status = ?,
                        permissions = ?, updated_at = ?
                    WHERE id = ? AND tenant_id = ?
                    """,
                    (user_id, account_name, access_token, status, perms_json, now, acc_id, tenant_id),
                )
            else:
                acc_id = str(uuid4())
                conn.execute(
                    """
                    INSERT INTO connected_accounts
                        (id, user_id, platform, account_id, account_name, access_token, status, permissions,
                         updated_at, created_at, tenant_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (acc_id, user_id, platform, account_id, account_name, access_token, status, perms_json, now, now, tenant_id),
                )
        return self.get_connected_account(user_id, platform, tenant_id=tenant_id, account_id=account_id)  # type: ignore[return-value]

    def get_connected_account(
        self, user_id: Optional[str], platform: str, *, tenant_id: str = DEFAULT_TENANT_ID, account_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM connected_accounts WHERE tenant_id = ? AND platform = ?"
        params: List[Any] = [tenant_id, platform.lower()]
        if user_id is not None:  # None => any user within the tenant (used by the scheduler)
            sql += " AND (user_id = ? OR user_id = 'global')"
            params.append(user_id)
        if account_id is not None:
            sql += " AND account_id = ?"
            params.append(account_id)
        sql += " ORDER BY updated_at DESC LIMIT 1"
        with self._conn() as conn:
            row = conn.execute(sql, params).fetchone()
            return self._account_row(row) if row else None

    def list_connected_accounts(
        self, user_id: Optional[str] = "global", *, tenant_id: str = DEFAULT_TENANT_ID
    ) -> List[Dict[str, Any]]:
        """``user_id=None`` lists every account of the tenant."""
        sql = "SELECT * FROM connected_accounts WHERE tenant_id = ?"
        params: List[Any] = [tenant_id]
        if user_id is not None:
            sql += " AND (user_id = ? OR user_id = 'global')"
            params.append(user_id)
        with self._conn() as conn:
            rows = conn.execute(sql + " ORDER BY updated_at DESC", params).fetchall()
            return [self._account_row(r) for r in rows]

    def delete_connected_account(self, account_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> bool:
        """Delete by the row id (``connected_accounts.id``) within the tenant."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM connected_accounts WHERE id = ? AND tenant_id = ?", (account_id, tenant_id))
            return cur.rowcount > 0

    def delete_connected_accounts_for_platform(self, platform: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM connected_accounts WHERE tenant_id = ? AND platform = ?", (tenant_id, platform.lower())
            )
            return cur.rowcount

    def list_connected_accounts_any_tenant(self, platform: str, account_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM connected_accounts WHERE platform = ? AND account_id = ? ORDER BY updated_at DESC",
                (platform.lower(), account_id),
            ).fetchall()
            return [self._account_row(r) for r in rows]

    # -------------------------------------------------- agency applications
    def save_agency_application(self, application: Dict[str, Any], *, tenant_id: str = DEFAULT_TENANT_ID) -> Dict[str, Any]:
        app_id = str(uuid4())
        now = iso_utc(utcnow())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO agency_applications
                    (id, tenant_id, user_id, company_name, credit_code, store_url, contact, remarks,
                     business_license_path, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    app_id,
                    tenant_id,
                    application.get("user_id"),
                    application["company_name"],
                    application["credit_code"],
                    application["store_url"],
                    application["contact"],
                    application.get("remarks"),
                    application.get("business_license_path"),
                    application.get("status") or "received",
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM agency_applications WHERE id = ?", (app_id,)).fetchone()
            return dict(row)

    def list_agency_applications(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM agency_applications WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ------------------------------------------------- subscriptions & usage
    def get_subscription(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
            return dict(row) if row else None

    def ensure_subscription(
        self, *, tenant_id: str, plan: str, billing_cycle: str, status: str, current_period_end: Optional[datetime]
    ) -> Dict[str, Any]:
        now = iso_utc(utcnow())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO subscriptions
                    (id, tenant_id, plan, billing_cycle, status, current_period_end, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), tenant_id, plan, billing_cycle, status, iso_utc(current_period_end), now, now),
            )
        return self.get_subscription(tenant_id=tenant_id)  # type: ignore[return-value]

    def save_subscription(
        self, *, tenant_id: str, plan: str, billing_cycle: str, status: str, current_period_end: Optional[datetime]
    ) -> Dict[str, Any]:
        now = iso_utc(utcnow())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO subscriptions
                    (id, tenant_id, plan, billing_cycle, status, current_period_end, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    plan = excluded.plan, billing_cycle = excluded.billing_cycle, status = excluded.status,
                    current_period_end = excluded.current_period_end, updated_at = excluded.updated_at
                """,
                (str(uuid4()), tenant_id, plan, billing_cycle, status, iso_utc(current_period_end), now, now),
            )
        return self.get_subscription(tenant_id=tenant_id)  # type: ignore[return-value]

    def get_usage(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM usage_tracking WHERE tenant_id = ?", (tenant_id,)).fetchone()
            return dict(row) if row else None

    def save_usage(
        self, *, tenant_id: str, ai_runs_count: int, cycle_start: datetime, cycle_end: datetime
    ) -> Dict[str, Any]:
        now = iso_utc(utcnow())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO usage_tracking (tenant_id, ai_runs_count, cycle_start, cycle_end, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    ai_runs_count = excluded.ai_runs_count, cycle_start = excluded.cycle_start,
                    cycle_end = excluded.cycle_end, updated_at = excluded.updated_at
                """,
                (tenant_id, ai_runs_count, iso_utc(cycle_start), iso_utc(cycle_end), now),
            )
        return self.get_usage(tenant_id=tenant_id)  # type: ignore[return-value]

    def increment_ai_runs(self, *, tenant_id: str, limit: Optional[int] = None) -> Optional[int]:
        moment = utcnow()
        params = {
            "tenant": tenant_id,
            "now": iso_utc(moment),
            "next_end": iso_utc(moment + timedelta(days=AI_USAGE_CYCLE_DAYS)),
            "limit": limit,
        }
        with self._conn() as conn:  # the first write takes SQLite's write lock: the two statements are atomic
            conn.execute(
                "INSERT OR IGNORE INTO usage_tracking (tenant_id, ai_runs_count, cycle_start, cycle_end, updated_at) "
                "VALUES (:tenant, 0, :now, :next_end, :now)",
                params,
            )
            rows = conn.execute(
                """
                UPDATE usage_tracking
                   SET ai_runs_count = CASE WHEN :now > cycle_end THEN 1 ELSE ai_runs_count + 1 END,
                       cycle_start   = CASE WHEN :now > cycle_end THEN :now ELSE cycle_start END,
                       cycle_end     = CASE WHEN :now > cycle_end THEN :next_end ELSE cycle_end END,
                       updated_at    = :now
                 WHERE tenant_id = :tenant
                   AND (:limit IS NULL OR :now > cycle_end OR ai_runs_count < :limit)
                RETURNING ai_runs_count
                """,
                params,
            ).fetchall()
        return int(rows[0][0]) if rows else None

    def release_ai_run(self, *, tenant_id: str) -> None:
        now = iso_utc(utcnow())
        with self._conn() as conn:
            conn.execute(
                "UPDATE usage_tracking SET ai_runs_count = ai_runs_count - 1, updated_at = ? "
                "WHERE tenant_id = ? AND ai_runs_count > 0 AND ? <= cycle_end",
                (now, tenant_id, now),
            )

    def save_upgrade_request(self, request: Dict[str, Any], *, tenant_id: str) -> Dict[str, Any]:
        request_id = str(uuid4())
        now = iso_utc(utcnow())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO upgrade_requests (id, tenant_id, user_id, plan, billing_cycle, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, tenant_id, request.get("user_id"), request["plan"], request["billing_cycle"],
                 request.get("status") or "pending", now, now),
            )
            row = conn.execute("SELECT * FROM upgrade_requests WHERE id = ?", (request_id,)).fetchone()
            return dict(row)

    def list_upgrade_requests(
        self, *, tenant_id: str = DEFAULT_TENANT_ID, status: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        query = "SELECT * FROM upgrade_requests WHERE tenant_id = ?"
        args: List[Any] = [tenant_id]
        if status:
            query += " AND status = ?"
            args.append(status)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(query + " ORDER BY created_at DESC", args).fetchall()]

    def list_upgrade_requests_any_tenant(self, *, status: Optional[str] = "pending") -> List[Dict[str, Any]]:
        query, args = "SELECT * FROM upgrade_requests", []
        if status:
            query, args = query + " WHERE status = ?", [status]
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(query + " ORDER BY created_at", args).fetchall()]

    def set_upgrade_requests_status(self, status: str, *, tenant_id: str, from_status: str = "pending") -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE upgrade_requests SET status = ?, updated_at = ? WHERE tenant_id = ? AND status = ?",
                (status, iso_utc(utcnow()), tenant_id, from_status),
            )
            return cur.rowcount

    # ------------------------------------------------------------ payments
    def apply_payment(
        self,
        payment: Dict[str, Any],
        *,
        tenant_id: str,
        activate: Callable[[Optional[Dict[str, Any]]], Optional[Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        now = iso_utc(utcnow())
        with self._conn() as conn:  # the INSERT takes SQLite's write lock: the whole block is serialised
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO billing_payments
                    (id, tenant_id, provider, plan, billing_cycle, amount, currency, status, reference,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'review', ?, ?, ?)
                """,
                (payment["id"], tenant_id, payment.get("provider") or "stripe", payment["plan"],
                 payment["billing_cycle"], int(payment["amount"]), payment["currency"], payment.get("reference"),
                 now, now),
            ).rowcount
            if not inserted:
                return None
            row = conn.execute("SELECT * FROM subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
            update = activate(dict(row) if row else None)
            status, period_end = "review", None
            if update is not None:
                period_end = iso_utc(update["current_period_end"])
                conn.execute(
                    """
                    INSERT INTO subscriptions
                        (id, tenant_id, plan, billing_cycle, status, current_period_end, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (tenant_id) DO UPDATE SET
                        plan = excluded.plan, billing_cycle = excluded.billing_cycle, status = excluded.status,
                        current_period_end = excluded.current_period_end, updated_at = excluded.updated_at
                    """,
                    (str(uuid4()), tenant_id, update["plan"], update["billing_cycle"], update["status"],
                     period_end, now, now),
                )
                status = "paid"
            conn.execute(
                "UPDATE billing_payments SET status = ?, period_end = ? WHERE id = ?", (status, period_end, payment["id"])
            )
            return dict(conn.execute("SELECT * FROM billing_payments WHERE id = ?", (payment["id"],)).fetchone())

    def list_payments(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM billing_payments WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
            ).fetchall()
            return [dict(r) for r in rows]

    # -------------------------------------------------------- page comments
    def save_page_comment(self, comment: Dict[str, Any], *, tenant_id: str = DEFAULT_TENANT_ID) -> Dict[str, Any]:
        now = iso_utc(utcnow())
        values = [comment.get(field) for field in _COMMENT_FIELDS]
        values[_COMMENT_FIELDS.index("verb")] = comment.get("verb") or "add"
        with self._conn() as conn:
            conn.execute(
                f"""
                INSERT INTO page_comments (id, tenant_id, {", ".join(_COMMENT_FIELDS)}, created_time, created_at, updated_at)
                VALUES (?, ?, {", ".join("?" for _ in _COMMENT_FIELDS)}, ?, ?, ?)
                ON CONFLICT (tenant_id, comment_id) DO UPDATE SET
                    page_id = excluded.page_id,
                    post_id = COALESCE(excluded.post_id, page_comments.post_id),
                    parent_id = COALESCE(excluded.parent_id, page_comments.parent_id),
                    from_id = COALESCE(excluded.from_id, page_comments.from_id),
                    from_name = COALESCE(excluded.from_name, page_comments.from_name),
                    message = COALESCE(excluded.message, page_comments.message),
                    verb = excluded.verb,
                    created_time = COALESCE(excluded.created_time, page_comments.created_time),
                    updated_at = excluded.updated_at
                """,
                (str(uuid4()), tenant_id, *values, iso_utc(comment.get("created_time")), now, now),
            )
            row = conn.execute(
                "SELECT * FROM page_comments WHERE tenant_id = ? AND comment_id = ?", (tenant_id, comment["comment_id"])
            ).fetchone()
            return dict(row)

    def get_page_comment(self, comment_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM page_comments WHERE tenant_id = ? AND comment_id = ?", (tenant_id, comment_id)
            ).fetchone()
            return dict(row) if row else None

    def list_page_comments(
        self, *, tenant_id: str = DEFAULT_TENANT_ID, post_id: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM page_comments WHERE tenant_id = ?"
        params: List[Any] = [tenant_id]
        if post_id is not None:
            sql += " AND post_id = ?"
            params.append(post_id)
        sql += " ORDER BY COALESCE(created_time, created_at) DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    # ------------------------------------------------ campaigns/drafts/tasks
    @staticmethod
    def _dump(item: Any) -> str:
        return json.dumps(item.model_dump(mode="json"), ensure_ascii=False)

    @staticmethod
    def _guard(cur: sqlite3.Cursor, table: str, item_id: str, tenant_id: str) -> None:
        if cur.rowcount == 0:
            raise CrossTenantWriteError(f"{table} row {item_id} belongs to another tenant (writer tenant={tenant_id})")

    def save_campaign(self, item: Campaign) -> Campaign:
        item.updated_at = utcnow()
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO campaigns (id, tenant_id, payload, created_at, updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                WHERE campaigns.tenant_id = excluded.tenant_id
                """,
                (item.id, item.tenant_id, self._dump(item), iso_utc(item.created_at), iso_utc(item.updated_at)),
            )
            self._guard(cur, "campaigns", item.id, item.tenant_id)
        return item

    def save_draft(self, item: ContentDraft) -> ContentDraft:
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO drafts (id, tenant_id, campaign_id, payload, created_at) VALUES (?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, campaign_id = excluded.campaign_id
                WHERE drafts.tenant_id = excluded.tenant_id
                """,
                (item.id, item.tenant_id, item.campaign_id, self._dump(item), iso_utc(item.created_at)),
            )
            self._guard(cur, "drafts", item.id, item.tenant_id)
        return item

    def save_task(self, item: PublishTask) -> PublishTask:
        now = utcnow()
        item.updated_at = now
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO tasks (id, tenant_id, campaign_id, payload, status, scheduled_at, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, status = excluded.status,
                    scheduled_at = excluded.scheduled_at, campaign_id = excluded.campaign_id,
                    updated_at = excluded.updated_at
                WHERE tasks.tenant_id = excluded.tenant_id
                """,
                (
                    item.id,
                    item.tenant_id,
                    item.campaign_id,
                    self._dump(item),
                    item.status.value,
                    iso_utc(item.scheduled_at),
                    iso_utc(now),
                    iso_utc(now),
                ),
            )
            self._guard(cur, "tasks", item.id, item.tenant_id)
        return item

    def _get_payload(self, table: str, item_id: str, tenant_id: Optional[str]) -> Optional[dict]:
        with self._conn() as conn:
            if tenant_id is None:
                row = conn.execute(f"SELECT payload, tenant_id FROM {table} WHERE id = ?", (item_id,)).fetchone()
            else:
                row = conn.execute(
                    f"SELECT payload, tenant_id FROM {table} WHERE id = ? AND tenant_id = ?", (item_id, tenant_id)
                ).fetchone()
        if not row:
            return None
        data = json.loads(row["payload"])
        data["tenant_id"] = row["tenant_id"]  # column is authoritative (legacy payloads lack it)
        return data

    def _list_payloads(self, table: str, tenant_id: str, extra_sql: str = "", params: tuple = ()) -> List[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT payload, tenant_id FROM {table} WHERE tenant_id = ?{extra_sql} ORDER BY rowid DESC",
                (tenant_id, *params),
            ).fetchall()
        result = []
        for row in rows:
            data = json.loads(row["payload"])
            data["tenant_id"] = row["tenant_id"]
            result.append(data)
        return result

    def get_campaign(self, item_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[Campaign]:
        data = self._get_payload("campaigns", item_id, tenant_id)
        return Campaign.model_validate(data) if data else None

    def get_draft(self, item_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[ContentDraft]:
        data = self._get_payload("drafts", item_id, tenant_id)
        return ContentDraft.model_validate(data) if data else None

    def get_task(self, item_id: str, *, tenant_id: str = DEFAULT_TENANT_ID) -> Optional[PublishTask]:
        data = self._get_payload("tasks", item_id, tenant_id)
        return PublishTask.model_validate(data) if data else None

    def list_campaigns(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[Campaign]:
        return [Campaign.model_validate(d) for d in self._list_payloads("campaigns", tenant_id)]

    def list_drafts(self, *, tenant_id: str = DEFAULT_TENANT_ID) -> List[ContentDraft]:
        return [ContentDraft.model_validate(d) for d in self._list_payloads("drafts", tenant_id)]

    def list_tasks(self, *, tenant_id: str = DEFAULT_TENANT_ID, status: Optional[str] = None) -> List[PublishTask]:
        if status:
            payloads = self._list_payloads("tasks", tenant_id, " AND status = ?", (status,))
        else:
            payloads = self._list_payloads("tasks", tenant_id)
        return [PublishTask.model_validate(d) for d in payloads]

    def counts(self, *, tenant_id: Optional[str] = None) -> Dict[str, int]:
        tables = ["campaigns", "drafts", "tasks", "users", "connected_accounts"]
        result: Dict[str, int] = {}
        with self._conn() as conn:
            for table in tables:
                if tenant_id is None:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                else:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?", (tenant_id,)).fetchone()
                result[table] = int(row[0])
        return result

    # ------------------------------------------------ scheduler (worker only)
    def get_task_any_tenant(self, item_id: str) -> Optional[PublishTask]:
        data = self._get_payload("tasks", item_id, None)
        return PublishTask.model_validate(data) if data else None

    def get_draft_any_tenant(self, item_id: str) -> Optional[ContentDraft]:
        data = self._get_payload("drafts", item_id, None)
        return ContentDraft.model_validate(data) if data else None

    def _tasks_where(self, where: str, params: tuple, limit: Optional[int] = None) -> List[PublishTask]:
        sql = f"SELECT payload, tenant_id FROM tasks WHERE {where}"
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        tasks = []
        for row in rows:
            data = json.loads(row["payload"])
            data["tenant_id"] = row["tenant_id"]
            tasks.append(PublishTask.model_validate(data))
        return tasks

    def list_due_tasks(self, now: datetime, limit: int = 50) -> List[PublishTask]:
        return self._tasks_where(
            "status = ? AND scheduled_at IS NOT NULL AND scheduled_at <= ? ORDER BY scheduled_at",
            (PublishStatus.SCHEDULED.value, iso_utc(now)),
            limit,
        )

    def list_scheduled_tasks(self) -> List[PublishTask]:
        return self._tasks_where("status = ? ORDER BY scheduled_at", (PublishStatus.SCHEDULED.value,))

    def list_recoverable_tasks(self, stale_before: datetime) -> List[PublishTask]:
        return self._tasks_where(
            "status = ? AND (updated_at IS NULL OR updated_at < ?)",
            (PublishStatus.PUBLISHING.value, iso_utc(stale_before)),
        )

    def claim_task(self, task_id: str, *, expected_status: str, new_status: str) -> bool:
        """Atomic compare-and-swap on ``status``; exactly one concurrent caller wins.

        ``BEGIN IMMEDIATE`` takes SQLite's write lock before reading, so read-check-write is one
        atomic step across threads and processes; the ``AND status = ?`` guard is defence in depth.
        """
        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT payload, status, tenant_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row or row["status"] != expected_status:
                return False
            data = json.loads(row["payload"])
            data["tenant_id"] = row["tenant_id"]
            task = PublishTask.model_validate(data)
            now = utcnow()
            task.status = PublishStatus(new_status)
            task.updated_at = now
            if new_status == PublishStatus.PUBLISHING.value:
                task.attempts += 1
            cur = conn.execute(
                "UPDATE tasks SET status = ?, payload = ?, updated_at = ? WHERE id = ? AND status = ?",
                (new_status, self._dump(task), iso_utc(now), task_id, expected_status),
            )
            return cur.rowcount == 1
