"""PostgreSQL-specific checks: schema idempotency and Row Level Security.

Skipped unless TEST_DATABASE_URL points at a disposable PostgreSQL database (the generic store
contract in test_storage_tenancy.py also runs against it when set).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set (no PostgreSQL available)")

SQL_FILE = Path(__file__).resolve().parent.parent / "scripts" / "init_supabase.sql"
APP_ROLE = "omniflow_rls_test"


def _psycopg():
    import psycopg

    return psycopg


@pytest.fixture
def owner():
    psycopg = _psycopg()
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(SQL_FILE.read_text(encoding="utf-8"))
        conn.execute("TRUNCATE tenants, users, campaigns, content_drafts, social_accounts, scheduled_tasks CASCADE")
        conn.execute("INSERT INTO tenants (id, name, slug) VALUES ('default', 'Default Workspace', 'default')")
        yield conn


@pytest.fixture
def app_role_url(owner):
    """A least-privilege, non-owner role: RLS applies to it (owners bypass unless FORCE is set)."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    owner.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")
    owner.execute(f"CREATE ROLE {APP_ROLE} LOGIN PASSWORD 'rlspw' NOSUPERUSER NOBYPASSRLS")
    owner.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    owner.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    info = conninfo_to_dict(PG_URL)
    yield make_conninfo(**{**info, "user": APP_ROLE, "password": "rlspw"})
    owner.execute(f"REASSIGN OWNED BY {APP_ROLE} TO CURRENT_USER")
    owner.execute(f"DROP OWNED BY {APP_ROLE}")
    owner.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")


def test_schema_script_is_idempotent(owner):
    sql = SQL_FILE.read_text(encoding="utf-8")
    for _ in range(3):
        owner.execute(sql)
    tables = {r[0] for r in owner.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
    assert {"tenants", "users", "campaigns", "content_drafts", "social_accounts", "scheduled_tasks"} <= tables
    assert owner.execute("SELECT count(*) FROM tenants WHERE id='default'").fetchone()[0] == 1


def test_updated_at_trigger_fires(owner):
    owner.execute("INSERT INTO tenants (id, name) VALUES ('t1', 'one')")
    before = owner.execute("SELECT updated_at FROM tenants WHERE id='t1'").fetchone()[0]
    owner.execute("SELECT pg_sleep(0.05)")
    owner.execute("UPDATE tenants SET name='renamed' WHERE id='t1'")
    after = owner.execute("SELECT updated_at FROM tenants WHERE id='t1'").fetchone()[0]
    assert after > before


def test_status_check_constraint_rejects_unknown_values(owner):
    psycopg = _psycopg()
    with pytest.raises(psycopg.errors.CheckViolation):
        owner.execute(
            "INSERT INTO scheduled_tasks (id, tenant_id, platform, status, payload) VALUES ('x','default','meta','bogus','{}')"
        )


def test_tenant_deletion_cascades(owner):
    owner.execute("INSERT INTO tenants (id, name) VALUES ('gone', 'gone')")
    owner.execute("INSERT INTO campaigns (id, tenant_id, name, payload) VALUES ('c', 'gone', 'n', '{}')")
    owner.execute("DELETE FROM tenants WHERE id='gone'")
    assert owner.execute("SELECT count(*) FROM campaigns WHERE id='c'").fetchone()[0] == 0


def test_partial_indexes_exist(owner):
    names = {r[0] for r in owner.execute("SELECT indexname FROM pg_indexes WHERE tablename='scheduled_tasks'")}
    assert {"ix_scheduled_tasks_due", "ix_scheduled_tasks_stuck", "ix_scheduled_tasks_tenant"} <= names


def test_rls_blocks_cross_tenant_access_for_non_owner_role(owner, app_role_url):
    psycopg = _psycopg()
    owner.execute("INSERT INTO tenants (id, name) VALUES ('a','A'), ('b','B')")
    owner.execute("INSERT INTO campaigns (id, tenant_id, name, payload) VALUES ('ca','a','A camp','{}'), ('cb','b','B camp','{}')")

    with psycopg.connect(app_role_url, autocommit=False) as conn:
        # No tenant context at all -> nothing visible (fail closed).
        assert conn.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0
        conn.rollback()

        conn.execute("SELECT set_config('app.tenant_id', 'a', true)")
        assert [r[0] for r in conn.execute("SELECT id FROM campaigns ORDER BY id")] == ["ca"]
        # ... even when the query explicitly asks for the other tenant's row
        assert conn.execute("SELECT count(*) FROM campaigns WHERE tenant_id='b'").fetchone()[0] == 0
        # Writes into another tenant are rejected by WITH CHECK
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO campaigns (id, tenant_id, name, payload) VALUES ('evil','b','x','{}')")
        conn.rollback()

        # Updates/deletes silently affect zero foreign rows
        conn.execute("SELECT set_config('app.tenant_id', 'a', true)")
        assert conn.execute("UPDATE campaigns SET name='pwned' WHERE id='cb'").rowcount == 0
        assert conn.execute("DELETE FROM campaigns WHERE id='cb'").rowcount == 0
        conn.rollback()

        # Context does not leak across transactions (set_config is transaction-local)
        assert conn.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0
        conn.rollback()

        # The worker's explicit cross-tenant scope
        conn.execute("SELECT set_config('app.bypass_rls', 'on', true)")
        assert conn.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 2

    assert owner.execute("SELECT name FROM campaigns WHERE id='cb'").fetchone()[0] == "B camp"


def test_rls_applies_to_every_tenant_table(owner):
    rows = owner.execute(
        "SELECT relname, relrowsecurity FROM pg_class WHERE relname = ANY(%s)",
        (["tenants", "users", "campaigns", "content_drafts", "social_accounts", "scheduled_tasks"],),
    ).fetchall()
    assert len(rows) == 6 and all(enabled for _, enabled in rows)


def test_postgres_store_end_to_end_as_non_owner_role(owner, app_role_url):
    """The application store works (and stays isolated) when connected as the RLS-subject role."""
    from db.postgres_store import PostgresStore
    from models.schemas import Campaign, CampaignCreate

    owner.execute("INSERT INTO tenants (id, name) VALUES ('a','A'), ('b','B')")
    store = PostgresStore(app_role_url, min_size=1, max_size=4)
    try:
        data = CampaignCreate(name="via app role").model_dump()
        data.update(tenant_id="a", id="camp-rls")
        store.save_campaign(Campaign(**data))
        assert store.get_campaign("camp-rls", tenant_id="a").name == "via app role"
        assert store.get_campaign("camp-rls", tenant_id="b") is None
        assert store.list_campaigns(tenant_id="b") == []
    finally:
        store.close()
