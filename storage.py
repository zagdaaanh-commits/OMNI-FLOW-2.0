from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from uuid import uuid4

from models.schemas import AnalyticsReport, Campaign, ContentDraft, PublishTask


class SQLiteStore:
    """Production-ready SQLite persistence layer with User Authentication & Social Connections."""

    def __init__(self, path: str = "./data/marketing.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._seed_default_user()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
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
                """
            )

    # -------------------------------------------------------------------------
    # Password Hashing & Verification
    # -------------------------------------------------------------------------
    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_hex(16)
        hashed = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
        return f"{salt}${hashed}"

    @staticmethod
    def verify_password(password: str, stored_hash: str) -> bool:
        try:
            salt, hashed = stored_hash.split("$", 1)
            check = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
            return secrets.compare_digest(hashed, check)
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # User Management
    # -------------------------------------------------------------------------
    def _seed_default_user(self) -> None:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM users").fetchone()
            if row[0] == 0:
                uid = str(uuid4())
                pwd_hash = self.hash_password("admin123")
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO users (id, email, full_name, password_hash, role, company, avatar_url, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (uid, "admin@omniflow.ai", "Alex Rivera", pwd_hash, "Brand Director", "Global Brand HQ", "", now),
                )

    def create_user(
        self,
        email: str,
        full_name: str,
        password: str,
        role: str = "Brand Lead",
        company: str = "Global Brand HQ",
        avatar_url: str = "",
    ) -> Dict[str, Any]:
        email = email.strip().lower()
        full_name = full_name.strip()
        uid = str(uuid4())
        pwd_hash = self.hash_password(password)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO users (id, email, full_name, password_hash, role, company, avatar_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, email, full_name, pwd_hash, role, company, avatar_url, now),
            )
        return self.get_user_by_id(uid)

    def get_user_by_email(self, email: str) -> Dict[str, Any] | None:
        email = email.strip().lower()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE lower(email) = ?", (email,)).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: str) -> Dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d.pop("password_hash", None)
            return d

    def list_users(self) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, email, full_name, role, company, avatar_url, created_at FROM users").fetchall()
            return [dict(r) for r in rows]

    def authenticate_user(self, email: str, password: str) -> Dict[str, Any] | None:
        user = self.get_user_by_email(email)
        if not user:
            return None
        if not self.verify_password(password, user["password_hash"]):
            return None
        user.pop("password_hash", None)
        return user

    # -------------------------------------------------------------------------
    # Connected Social Accounts
    # -------------------------------------------------------------------------
    def save_connected_account(
        self,
        user_id: str,
        platform: str,
        account_id: str,
        account_name: str,
        access_token: str,
        status: str = "connected",
        permissions: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        platform = platform.lower()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM connected_accounts WHERE user_id = ? AND platform = ?",
                (user_id, platform),
            ).fetchone()
            acc_id = row["id"] if row else str(uuid4())
            now = datetime.now(timezone.utc).isoformat()
            perms_json = json.dumps(permissions or ["publish_posts", "read_insights"])
            conn.execute(
                """
                INSERT OR REPLACE INTO connected_accounts 
                (id, user_id, platform, account_id, account_name, access_token, status, permissions, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (acc_id, user_id, platform, account_id, account_name, access_token, status, perms_json, now),
            )
        return self.get_connected_account(user_id, platform)

    def get_connected_account(self, user_id: str, platform: str) -> Dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM connected_accounts WHERE (user_id = ? OR user_id = 'global') AND platform = ?",
                (user_id, platform.lower()),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get("permissions"):
                try:
                    d["permissions"] = json.loads(d["permissions"])
                except Exception:
                    pass
            t = d.get("access_token", "")
            d["masked_token"] = (t[:6] + "..." + t[-4:]) if len(t) > 10 else "***"
            return d

    def list_connected_accounts(self, user_id: str = "global") -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM connected_accounts WHERE user_id = ? OR user_id = 'global' ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            results = []
            for r in rows:
                d = dict(r)
                if d.get("permissions"):
                    try:
                        d["permissions"] = json.loads(d["permissions"])
                    except Exception:
                        pass
                t = d.get("access_token", "")
                d["masked_token"] = (t[:6] + "..." + t[-4:]) if len(t) > 10 else "***"
                results.append(d)
            return results

    def delete_connected_account(self, account_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM connected_accounts WHERE id = ?", (account_id,))
            return cur.rowcount > 0

    # -------------------------------------------------------------------------
    # Core Campaign, Draft, Task Persistence
    # -------------------------------------------------------------------------
    def save_campaign(self, item: Campaign) -> Campaign:
        payload = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO campaigns(id,payload,created_at,updated_at) VALUES(?,?,?,?)",
                (item.id, payload, item.created_at.isoformat(), item.updated_at.isoformat()),
            )
        return item

    def save_draft(self, item: ContentDraft) -> ContentDraft:
        payload = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO drafts(id,payload,created_at) VALUES(?,?,?)",
                (item.id, payload, item.created_at.isoformat()),
            )
        return item

    def save_task(self, item: PublishTask) -> PublishTask:
        payload = json.dumps(item.model_dump(mode="json"), ensure_ascii=False)
        timestamp = (item.published_at or item.scheduled_at).isoformat() if (item.published_at or item.scheduled_at) else ""
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO tasks(id,payload,status,updated_at) VALUES(?,?,?,?)",
                (item.id, payload, item.status.value, timestamp),
            )
        return item

    def get_campaign(self, item_id: str) -> Campaign | None:
        row = self._get("campaigns", item_id)
        return Campaign.model_validate(json.loads(row["payload"])) if row else None

    def get_draft(self, item_id: str) -> ContentDraft | None:
        row = self._get("drafts", item_id)
        return ContentDraft.model_validate(json.loads(row["payload"])) if row else None

    def get_task(self, item_id: str) -> PublishTask | None:
        row = self._get("tasks", item_id)
        return PublishTask.model_validate(json.loads(row["payload"])) if row else None

    def list_campaigns(self) -> list[Campaign]:
        return [Campaign.model_validate(json.loads(r["payload"])) for r in self._rows("campaigns")]

    def list_drafts(self) -> list[ContentDraft]:
        return [ContentDraft.model_validate(json.loads(r["payload"])) for r in self._rows("drafts")]

    def list_tasks(self) -> list[PublishTask]:
        return [PublishTask.model_validate(json.loads(r["payload"])) for r in self._rows("tasks")]

    def counts(self) -> dict[str, int]:
        return {
            "campaigns": self._count("campaigns"),
            "drafts": self._count("drafts"),
            "tasks": self._count("tasks"),
            "users": self._count("users"),
            "connected_accounts": self._count("connected_accounts"),
        }

    def _get(self, table: str, item_id: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,)).fetchone()

    def _rows(self, table: str) -> Iterable[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC").fetchall()

    def _count(self, table: str) -> int:
        with self._connect() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
