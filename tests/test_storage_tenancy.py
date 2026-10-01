"""Store contract tests: run against SQLite always, and against PostgreSQL when TEST_DATABASE_URL is set.

    TEST_DATABASE_URL=postgresql://user:pw@127.0.0.1:5432/omniflow_test pytest tests/test_storage_tenancy.py

The Postgres database must be disposable: every test truncates the application tables.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db.base import CrossTenantWriteError, scoped_id
from db.passwords import hash_password, needs_rehash, verify_password
from db.sqlite_store import SQLiteStore
from models.schemas import Campaign, CampaignCreate, ContentDraft, Platform, PublishStatus, PublishTask

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------- fixtures
@pytest.fixture(params=BACKENDS)
def store(request, tmp_path):
    if request.param == "sqlite":
        s = SQLiteStore(str(tmp_path / "contract.db"))
        yield s
        s.close()
        return
    import psycopg

    from db.postgres_store import PostgresStore

    migrate_sql = Path(__file__).resolve().parent.parent / "scripts" / "init_supabase.sql"
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(migrate_sql.read_text(encoding="utf-8"))
        conn.execute("TRUNCATE tenants, users, campaigns, content_drafts, social_accounts, scheduled_tasks CASCADE")
    s = PostgresStore(PG_URL, min_size=1, max_size=12)
    yield s
    s.close()


def _campaign(tenant: str = "default", cid: str | None = None, name: str = "C") -> Campaign:
    data = CampaignCreate(name=name).model_dump()
    data["tenant_id"] = tenant
    if cid:
        data["id"] = cid
    return Campaign(**data)


def _draft(tenant: str = "default", did: str | None = None, cid: str = "camp") -> ContentDraft:
    kw = dict(tenant_id=tenant, campaign_id=cid, platform=Platform.META, language="en", body="body")
    if did:
        kw["id"] = did
    return ContentDraft(**kw)


def _task(tenant="default", tid=None, status=PublishStatus.SCHEDULED, at=NOW, draft_id="d1") -> PublishTask:
    kw = dict(tenant_id=tenant, campaign_id="camp", content_draft_id=draft_id, platform=Platform.META, status=status, scheduled_at=at)
    if tid:
        kw["id"] = tid
    return PublishTask(**kw)


def _mk_tenants(store, *names):
    return [store.create_tenant(n)["id"] for n in names]


# ---------------------------------------------------------------------- tenants
def test_default_tenant_exists(store):
    assert store.get_tenant("default")["id"] == "default"


def test_create_tenant_generates_unique_slugs(store):
    a, b = store.create_tenant("Acme Corp"), store.create_tenant("Acme Corp")
    assert a["id"] != b["id"] and a["slug"] == "acme-corp" and b["slug"] == "acme-corp-2"


def test_ensure_tenant_is_idempotent(store):
    first = store.ensure_tenant("workspace-1", "WS")
    again = store.ensure_tenant("workspace-1", "Other name")
    assert first["id"] == again["id"] == "workspace-1" and again["name"] == "WS"


# -------------------------------------------------------------------- isolation
def test_campaign_draft_task_isolation(store):
    a, b = _mk_tenants(store, "A", "B")
    store.save_campaign(_campaign(a, "camp-a"))
    store.save_draft(_draft(a, "draft-a"))
    store.save_task(_task(a, "task-a"))

    assert store.get_campaign("camp-a", tenant_id=a).id == "camp-a"
    assert store.get_campaign("camp-a", tenant_id=b) is None
    assert store.get_draft("draft-a", tenant_id=b) is None
    assert store.get_task("task-a", tenant_id=b) is None
    assert store.list_campaigns(tenant_id=b) == [] and store.list_drafts(tenant_id=b) == [] and store.list_tasks(tenant_id=b) == []
    assert [c.id for c in store.list_campaigns(tenant_id=a)] == ["camp-a"]
    assert store.get_campaign("camp-a").id if store.get_campaign("camp-a") else True  # default tenant: not visible
    assert store.get_campaign("camp-a") is None  # default tenant cannot see tenant A's row


def test_tenant_id_is_returned_on_models(store):
    (a,) = _mk_tenants(store, "A")
    store.save_campaign(_campaign(a, "c1"))
    assert store.get_campaign("c1", tenant_id=a).tenant_id == a
    store.save_task(_task(a, "t1"))
    assert store.list_tasks(tenant_id=a)[0].tenant_id == a


def test_cross_tenant_overwrite_is_refused_and_row_unchanged(store):
    a, b = _mk_tenants(store, "A", "B")
    store.save_campaign(_campaign(a, "shared-id", name="original"))
    with pytest.raises(CrossTenantWriteError):
        store.save_campaign(_campaign(b, "shared-id", name="hijack"))
    assert store.get_campaign("shared-id", tenant_id=a).name == "original"
    assert store.get_campaign("shared-id", tenant_id=b) is None

    store.save_draft(_draft(a, "dd"))
    with pytest.raises(CrossTenantWriteError):
        store.save_draft(_draft(b, "dd"))
    store.save_task(_task(a, "tt"))
    with pytest.raises(CrossTenantWriteError):
        store.save_task(_task(b, "tt", status=PublishStatus.FAILED))
    assert store.get_task("tt", tenant_id=a).status == PublishStatus.SCHEDULED


def test_same_tenant_upsert_updates(store):
    store.save_campaign(_campaign("default", "c-up", name="v1"))
    store.save_campaign(_campaign("default", "c-up", name="v2"))
    assert len(store.list_campaigns()) == 1 and store.get_campaign("c-up").name == "v2"


def test_scoped_id_namespaces_non_default_tenants():
    assert scoped_id("string", "default") == "string"
    assert scoped_id("string", "acme") == "acme:string"
    assert scoped_id("acme:string", "acme") == "acme:string"


def test_counts_per_tenant(store):
    a, b = _mk_tenants(store, "A", "B")
    store.save_campaign(_campaign(a, "c1")); store.save_campaign(_campaign(a, "c2")); store.save_campaign(_campaign(b, "c3"))
    assert store.counts(tenant_id=a)["campaigns"] == 2 and store.counts(tenant_id=b)["campaigns"] == 1
    assert store.counts()["campaigns"] == 3


def test_list_tasks_status_filter(store):
    store.save_task(_task("default", "s1", PublishStatus.SCHEDULED))
    store.save_task(_task("default", "f1", PublishStatus.FAILED))
    assert [t.id for t in store.list_tasks(status="failed")] == ["f1"]
    assert len(store.list_tasks()) == 2


# ---------------------------------------------------------------------- users
def test_users_are_tenant_scoped_but_login_is_global(store):
    a, b = _mk_tenants(store, "A", "B")
    ua = store.create_user("Alice@Example.com", "Alice", "pw-alice-1", tenant_id=a)
    store.create_user("bob@example.com", "Bob", "pw-bob-1", tenant_id=b)
    assert ua["tenant_id"] == a and ua["email"] == "alice@example.com" and "password_hash" not in ua
    assert [u["email"] for u in store.list_users(tenant_id=a)] == ["alice@example.com"]
    assert store.get_user_by_id(ua["id"], tenant_id=b) is None
    assert store.get_user_by_id(ua["id"], tenant_id=a)["full_name"] == "Alice"
    auth = store.authenticate_user("ALICE@example.com", "pw-alice-1")
    assert auth["tenant_id"] == a and "password_hash" not in auth
    assert store.authenticate_user("alice@example.com", "wrong") is None
    assert store.authenticate_user("nobody@example.com", "x") is None


def test_email_is_unique_across_tenants(store):
    from db import integrity_errors

    a, b = _mk_tenants(store, "A", "B")
    store.create_user("dup@example.com", "One", "pw12345", tenant_id=a)
    with pytest.raises(integrity_errors()):
        store.create_user("DUP@example.com", "Two", "pw12345", tenant_id=b)


def test_passwords_are_stored_as_pbkdf2(store):
    (a,) = _mk_tenants(store, "A")
    store.create_user("p@example.com", "P", "s3cret-pw", tenant_id=a)
    stored = store.get_user_by_email("p@example.com")["password_hash"]
    assert stored.startswith("pbkdf2_sha256$") and "s3cret-pw" not in stored
    assert verify_password("s3cret-pw", stored) and not verify_password("nope", stored)
    assert not needs_rehash(stored)


def test_legacy_sha256_hash_verifies_and_is_upgraded_on_login(store):
    import hashlib

    (a,) = _mk_tenants(store, "A")
    store.create_user("legacy@example.com", "L", "old-pw-123", tenant_id=a)
    salt = "abcdef0123456789"
    legacy = f"{salt}${hashlib.sha256((salt + 'old-pw-123').encode()).hexdigest()}"
    _set_password_hash(store, "legacy@example.com", legacy)
    assert verify_password("old-pw-123", legacy) and needs_rehash(legacy)
    assert store.authenticate_user("legacy@example.com", "old-pw-123") is not None
    assert store.get_user_by_email("legacy@example.com")["password_hash"].startswith("pbkdf2_sha256$")
    assert store.authenticate_user("legacy@example.com", "old-pw-123") is not None  # still works after rehash


def _set_password_hash(store, email: str, value: str) -> None:
    if isinstance(store, SQLiteStore):
        with store._conn() as conn:
            conn.execute("UPDATE users SET password_hash = ? WHERE lower(email) = ?", (value, email))
    else:
        with store._tx(None) as conn:
            conn.execute("UPDATE users SET password_hash = %s WHERE lower(email) = %s", (value, email))


def test_pbkdf2_hashes_are_salted():
    assert hash_password("same") != hash_password("same")
    assert not verify_password("x", "") and not verify_password("x", "garbage") and not verify_password("x", "pbkdf2_sha256$bad")


# ------------------------------------------------------------ social accounts
def test_connected_accounts_are_tenant_scoped_and_multi_page(store):
    a, b = _mk_tenants(store, "A", "B")
    store.save_connected_account("u1", "meta", "P1", "Page One", "TOKEN-ONE-123456", tenant_id=a)
    store.save_connected_account("u1", "meta", "P2", "Page Two", "TOKEN-TWO-123456", tenant_id=a)
    store.save_connected_account("u9", "meta", "P1", "Other tenant same page id", "TOKEN-OTHER-1234", tenant_id=b)

    assert {x["account_id"] for x in store.list_connected_accounts(None, tenant_id=a)} == {"P1", "P2"}
    assert store.get_connected_account(None, "meta", tenant_id=a)["account_id"] == "P2"  # most recently saved
    assert store.get_connected_account(None, "meta", tenant_id=a, account_id="P1")["account_name"] == "Page One"
    assert store.get_connected_account(None, "meta", tenant_id=b, account_id="P2") is None
    acc = store.get_connected_account(None, "meta", tenant_id=b)
    assert acc["account_name"] == "Other tenant same page id" and acc["masked_token"] == "TOKEN-...1234"
    assert store.list_connected_accounts(None, tenant_id="default") == []


def test_saving_same_page_twice_updates_in_place(store):
    (a,) = _mk_tenants(store, "A")
    first = store.save_connected_account("u", "meta", "P1", "Old", "OLD-TOKEN-12345", tenant_id=a)
    second = store.save_connected_account("u", "meta", "P1", "New", "NEW-TOKEN-12345", permissions=["x"], tenant_id=a)
    assert first["id"] == second["id"] and len(store.list_connected_accounts(None, tenant_id=a)) == 1
    assert second["account_name"] == "New" and second["permissions"] == ["x"]


def test_user_filter_and_global_semantics(store):
    store.save_connected_account("global", "meta", "G", "Global page", "GLOBAL-TOKEN-123")
    store.save_connected_account("u2", "tiktok", "T", "TT", "TIKTOK-TOKEN-1234")
    assert {x["account_id"] for x in store.list_connected_accounts("global")} == {"G"}
    assert {x["account_id"] for x in store.list_connected_accounts("u2")} == {"G", "T"}
    assert store.get_connected_account("someone", "meta")["account_id"] == "G"


def test_delete_connected_accounts_is_tenant_scoped(store):
    a, b = _mk_tenants(store, "A", "B")
    acc = store.save_connected_account("u", "meta", "P1", "n", "TOKEN-A-1234567", tenant_id=a)
    store.save_connected_account("u", "meta", "P1", "n", "TOKEN-B-1234567", tenant_id=b)
    assert store.delete_connected_account(acc["id"], tenant_id=b) is False
    assert store.delete_connected_account(acc["id"], tenant_id=a) is True
    assert store.delete_connected_accounts_for_platform("meta", tenant_id=a) == 0
    assert store.delete_connected_accounts_for_platform("meta", tenant_id=b) == 1


# ------------------------------------------------------------------ scheduler
def test_list_due_tasks_is_cross_tenant_ordered_and_filtered(store):
    a, b = _mk_tenants(store, "A", "B")
    store.save_task(_task(a, "late", at=NOW - timedelta(minutes=1)))
    store.save_task(_task(b, "earlier", at=NOW - timedelta(hours=1)))
    store.save_task(_task(a, "future", at=NOW + timedelta(hours=1)))
    store.save_task(_task(a, "no-time", at=None))
    store.save_task(_task(a, "done", status=PublishStatus.PUBLISHED, at=NOW - timedelta(hours=2)))
    store.save_task(_task(b, "publishing", status=PublishStatus.PUBLISHING, at=NOW - timedelta(hours=2)))

    due = store.list_due_tasks(NOW)
    assert [t.id for t in due] == ["earlier", "late"]
    assert {t.tenant_id for t in due} == {a, b}
    assert [t.id for t in store.list_due_tasks(NOW, limit=1)] == ["earlier"]


def test_any_tenant_getters_are_explicit_worker_paths(store):
    (a,) = _mk_tenants(store, "A")
    store.save_task(_task(a, "tx")); store.save_draft(_draft(a, "dx"))
    assert store.get_task("tx") is None and store.get_draft("dx") is None
    assert store.get_task_any_tenant("tx").tenant_id == a and store.get_draft_any_tenant("dx").tenant_id == a
    assert store.get_task_any_tenant("missing") is None


def test_claim_task_is_a_compare_and_swap(store):
    (a,) = _mk_tenants(store, "A")
    store.save_task(_task(a, "c1"))
    assert store.claim_task("c1", expected_status="scheduled", new_status="publishing") is True
    assert store.claim_task("c1", expected_status="scheduled", new_status="publishing") is False
    claimed = store.get_task("c1", tenant_id=a)
    assert claimed.status == PublishStatus.PUBLISHING and claimed.attempts == 1
    assert store.claim_task("missing", expected_status="scheduled", new_status="publishing") is False
    assert store.list_due_tasks(NOW) == []  # no longer due once claimed


def test_claim_task_has_exactly_one_winner_under_concurrency(store):
    (a,) = _mk_tenants(store, "A")
    store.save_task(_task(a, "race"))
    wins, errors = [], []
    barrier = threading.Barrier(12)

    def worker():
        try:
            barrier.wait()
            wins.append(store.claim_task("race", expected_status="scheduled", new_status="publishing"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert not errors, errors
    assert wins.count(True) == 1 and wins.count(False) == 11
    assert store.get_task("race", tenant_id=a).attempts == 1


def test_recovery_returns_stuck_publishing_tasks(store):
    (a,) = _mk_tenants(store, "A")
    store.save_task(_task(a, "stuck", status=PublishStatus.PUBLISHING))
    store.save_task(_task(a, "fresh-sched"))
    assert [t.id for t in store.list_recoverable_tasks(datetime.now(timezone.utc) + timedelta(minutes=1))] == ["stuck"]
    assert store.list_recoverable_tasks(datetime.now(timezone.utc) - timedelta(hours=1)) == []
    assert store.claim_task("stuck", expected_status="publishing", new_status="scheduled") is True
    assert store.get_task("stuck", tenant_id=a).status == PublishStatus.SCHEDULED


def test_save_task_always_stamps_updated_at(store):
    t = store.save_task(_task("default", "u1"))
    assert t.updated_at is not None
    assert store.get_task("u1").updated_at is not None


# --------------------------------------------------------------- SQLite specifics
OLD_SCHEMA = """
CREATE TABLE campaigns (id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE drafts (id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL, password_hash TEXT NOT NULL,
                    role TEXT NOT NULL, company TEXT NOT NULL, avatar_url TEXT, created_at TEXT NOT NULL);
CREATE TABLE connected_accounts (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, platform TEXT NOT NULL, account_id TEXT NOT NULL,
                    account_name TEXT NOT NULL, access_token TEXT NOT NULL, status TEXT NOT NULL, permissions TEXT, updated_at TEXT NOT NULL);
"""


def test_old_single_tenant_database_is_migrated_in_place(tmp_path):
    import hashlib
    import json

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(OLD_SCHEMA)
    campaign = Campaign(**CampaignCreate(name="Legacy").model_dump())
    old_task = {
        "id": "old-task", "campaign_id": "camp", "content_draft_id": "d", "platform": "meta", "status": "scheduled",
        "scheduled_at": "2026-09-30T06:51:20.786029Z", "logs": [],
    }
    conn.execute("INSERT INTO campaigns VALUES (?,?,?,?)", (campaign.id, json.dumps(campaign.model_dump(mode="json")), "2026-09-01", "2026-09-01"))
    conn.execute("INSERT INTO tasks VALUES (?,?,?,?)", ("old-task", json.dumps(old_task), "scheduled", "2026-09-30T06:51:20.786029Z"))
    conn.execute("INSERT INTO drafts VALUES (?,?,?)", ("old-draft", json.dumps({"id": "old-draft", "campaign_id": "camp", "platform": "meta", "language": "en", "body": "b"}), "2026-09-01"))
    legacy_hash = "salt123$" + hashlib.sha256(b"salt123hunter2!").hexdigest()
    conn.execute("INSERT INTO users VALUES (?,?,?,?,?,?,?,?)", ("u-old", "old@example.com", "Old User", legacy_hash, "Lead", "Co", "", "2026-09-01"))
    conn.execute("INSERT INTO connected_accounts VALUES (?,?,?,?,?,?,?,?,?)", ("a-old", "global", "meta", "PG1", "Old Page", "OLD-TOKEN-123456", "connected", '["x"]', "2026-09-01"))
    conn.commit(); conn.close()

    store = SQLiteStore(str(db))  # migrates in place, never drops data
    assert store.get_campaign(campaign.id).name == "Legacy" and store.get_campaign(campaign.id).tenant_id == "default"
    assert store.get_task("old-task").status == PublishStatus.SCHEDULED
    assert store.get_draft("old-draft").tenant_id == "default"
    assert store.get_connected_account("global", "meta")["account_id"] == "PG1"
    assert store.authenticate_user("old@example.com", "hunter2!")["tenant_id"] == "default"  # legacy hash still works
    # normalized scheduler columns were backfilled from the JSON payload
    due = store.list_due_tasks(datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert [t.id for t in due] == ["old-task"]

    SQLiteStore(str(db))  # running the migration again is a no-op
    with sqlite3.connect(db) as check:
        cols = {r[1] for r in check.execute("PRAGMA table_info(tasks)")}
        assert {"tenant_id", "campaign_id", "scheduled_at", "created_at"} <= cols
        assert check.execute("SELECT COUNT(*) FROM tenants WHERE id='default'").fetchone()[0] == 1
        assert check.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_sqlite_starts_without_any_seeded_user(tmp_path, monkeypatch):
    for env in ("development", "production"):
        monkeypatch.setenv("APP_ENV", env)
        store = SQLiteStore(str(tmp_path / f"{env}.db"))
        assert store.get_user_by_email("admin@omniflow.ai") is None
        assert store.list_users(tenant_id="default") == []


def test_storage_module_still_exports_sqlitestore():
    from storage import SQLiteStore as Legacy

    assert Legacy is SQLiteStore
