-- =============================================================================
-- OmniFlow 2.0 - PostgreSQL / Supabase schema (multi-tenant, RLS-ready)
--
-- Idempotent: safe to run any number of times (CREATE ... IF NOT EXISTS,
-- DROP ... IF EXISTS before CREATE for triggers/policies).
-- Apply with:   python scripts/migrate.py        (uses DATABASE_URL)
--          or:  psql "$DATABASE_URL" -f scripts/init_supabase.sql
--
-- Table mapping from the SQLite prototype:
--   campaigns -> campaigns | drafts -> content_drafts
--   tasks -> scheduled_tasks | connected_accounts -> social_accounts
--
-- Tenant isolation
--   * every business table carries NOT NULL tenant_id -> tenants(id) ON DELETE CASCADE
--   * RLS policies allow a row only when tenant_id = app_current_tenant()
--     (session setting 'app.tenant_id', set per transaction by the app; on Supabase
--     also the 'tenant_id' claim in the caller's JWT app_metadata)
--   * the scheduler worker sets 'app.bypass_rls' = 'on' for cross-tenant polling.
--     That setting is honoured only by these policies; never expose raw SQL access
--     to untrusted clients on the application role.
--
-- Foreign keys intentionally OMITTED (and why)
--   * scheduled_tasks.campaign_id / content_draft_id and content_drafts.campaign_id:
--     the API accepts publish requests for draft ids that were synthesised on the fly
--     (demo/Swagger flows), so a strict FK would reject flows that work today.
--     Tenant integrity is still enforced through tenant_id.
--   * social_accounts.user_id: holds the literal 'global' for tenant-wide connections.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- The migration itself must be able to seed rows regardless of RLS.
SELECT set_config('app.bypass_rls', 'on', true);

-- ---------------------------------------------------------------- helpers ---
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

-- Session-level tenant context (works on any Postgres).
CREATE OR REPLACE FUNCTION app_bypass_rls() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT coalesce(current_setting('app.bypass_rls', true), '') = 'on'
$$;

-- app_current_tenant(): 'app.tenant_id' setting; on Supabase additionally falls back to
-- the tenant_id claim of the request JWT (auth.jwt()). The auth schema only exists on
-- Supabase, so the Supabase variant is created dynamically and only when available.
DO $do$
BEGIN
    IF to_regprocedure('auth.jwt()') IS NOT NULL THEN
        EXECUTE $fn$
            CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS text
            LANGUAGE sql STABLE AS $body$
                SELECT coalesce(
                    nullif(current_setting('app.tenant_id', true), ''),
                    auth.jwt() -> 'app_metadata' ->> 'tenant_id'
                )
            $body$
        $fn$;
    ELSE
        EXECUTE $fn$
            CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS text
            LANGUAGE sql STABLE AS $body$
                SELECT nullif(current_setting('app.tenant_id', true), '')
            $body$
        $fn$;
    END IF;
END
$do$;

-- ---------------------------------------------------------------- tenants ---
CREATE TABLE IF NOT EXISTS tenants (
    id          text PRIMARY KEY,
    name        text        NOT NULL,
    slug        text UNIQUE,
    plan        text        NOT NULL DEFAULT 'free',
    status      text        NOT NULL DEFAULT 'active',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

INSERT INTO tenants (id, name, slug)
VALUES ('default', 'Default Workspace', 'default')
ON CONFLICT (id) DO NOTHING;

-- ------------------------------------------------------------------ users ---
CREATE TABLE IF NOT EXISTS users (
    id             text PRIMARY KEY,
    tenant_id      text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    email          text        NOT NULL,
    full_name      text        NOT NULL,
    password_hash  text        NOT NULL,
    role           text        NOT NULL DEFAULT 'Brand Lead',
    company        text        NOT NULL DEFAULT 'Global Brand HQ',
    avatar_url     text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
-- Login is by email alone, so emails are unique platform-wide (case-insensitive).
-- (tenant_id, lower(email)) uniqueness is implied and therefore not repeated.
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_email_lower ON users (lower(email));
CREATE INDEX IF NOT EXISTS ix_users_tenant ON users (tenant_id, created_at);

-- -------------------------------------------------------------- campaigns ---
CREATE TABLE IF NOT EXISTS campaigns (
    id          text PRIMARY KEY,
    tenant_id   text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    name        text        NOT NULL,
    status      text        NOT NULL DEFAULT 'draft',
    payload     jsonb       NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_campaigns_tenant ON campaigns (tenant_id, created_at DESC);

-- --------------------------------------------------------- content_drafts ---
CREATE TABLE IF NOT EXISTS content_drafts (
    id           text PRIMARY KEY,
    tenant_id    text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    campaign_id  text,
    platform     text        NOT NULL,
    language     text        NOT NULL DEFAULT 'en',
    payload      jsonb       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_content_drafts_tenant ON content_drafts (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_content_drafts_campaign ON content_drafts (tenant_id, campaign_id);

-- -------------------------------------------------------- social_accounts ---
CREATE TABLE IF NOT EXISTS social_accounts (
    id            text PRIMARY KEY,
    tenant_id     text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id       text        NOT NULL DEFAULT 'global',
    platform      text        NOT NULL,
    account_id    text        NOT NULL,
    account_name  text        NOT NULL,
    access_token  text        NOT NULL,   -- encrypt at rest (Supabase Vault / pgsodium) for regulated deployments
    status        text        NOT NULL DEFAULT 'connected',
    permissions   jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ux_social_accounts_tenant_platform_account UNIQUE (tenant_id, platform, account_id)
);
CREATE INDEX IF NOT EXISTS ix_social_accounts_tenant ON social_accounts (tenant_id, platform, updated_at DESC);

-- -------------------------------------------------------- scheduled_tasks ---
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id                text PRIMARY KEY,
    tenant_id         text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    campaign_id       text,
    content_draft_id  text,
    platform          text        NOT NULL,
    status            text        NOT NULL,
    scheduled_at      timestamptz,
    published_at      timestamptz,
    external_post_id  text,
    attempts          integer     NOT NULL DEFAULT 0,
    error             text,
    payload           jsonb       NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
-- Status vocabulary is enforced idempotently (a table created by an older run gets it too).
ALTER TABLE scheduled_tasks DROP CONSTRAINT IF EXISTS ck_scheduled_tasks_status;
ALTER TABLE scheduled_tasks ADD CONSTRAINT ck_scheduled_tasks_status
    CHECK (status IN ('scheduled', 'publishing', 'published', 'failed', 'cancelled'));

CREATE INDEX IF NOT EXISTS ix_scheduled_tasks_tenant ON scheduled_tasks (tenant_id, created_at DESC);
-- The scheduler polls only rows that are due: a tiny partial index keeps that O(due).
CREATE INDEX IF NOT EXISTS ix_scheduled_tasks_due ON scheduled_tasks (scheduled_at) WHERE status = 'scheduled';
CREATE INDEX IF NOT EXISTS ix_scheduled_tasks_stuck ON scheduled_tasks (updated_at) WHERE status = 'publishing';

-- ---------------------------------------------------- agency_applications ---
-- Meta agency (Meetsocial / YinoLink) ad-account applications submitted from the UI.
CREATE TABLE IF NOT EXISTS agency_applications (
    id            text PRIMARY KEY,
    tenant_id     text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id       text,
    company_name  text        NOT NULL,
    credit_code   text        NOT NULL,   -- Unified Social Credit Code (营业执照)
    store_url     text        NOT NULL,
    contact       text        NOT NULL,
    remarks       text,
    status        text        NOT NULL DEFAULT 'received',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_agency_applications_tenant ON agency_applications (tenant_id, created_at DESC);

-- ---------------------------------------------------------- page_comments ---
-- Facebook Page comments received through the Meta webhook (one row per tenant + comment).
CREATE TABLE IF NOT EXISTS page_comments (
    id            text PRIMARY KEY,
    tenant_id     text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    page_id       text        NOT NULL,
    comment_id    text        NOT NULL,
    post_id       text,
    parent_id     text,
    from_id       text,
    from_name     text,
    message       text,
    verb          text        NOT NULL DEFAULT 'add',
    created_time  timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ux_page_comments_tenant_comment UNIQUE (tenant_id, comment_id)
);
CREATE INDEX IF NOT EXISTS ix_page_comments_tenant_post ON page_comments (tenant_id, post_id, created_time DESC);
-- Webhook routing looks a Page up across tenants.
CREATE INDEX IF NOT EXISTS ix_social_accounts_platform_account ON social_accounts (platform, account_id);

-- --------------------------------------------- updated_at triggers (all) ---
DO $do$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['tenants', 'users', 'campaigns', 'content_drafts', 'social_accounts', 'scheduled_tasks',
                         'agency_applications', 'page_comments']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%1$s_updated_at ON %1$I', t);
        EXECUTE format(
            'CREATE TRIGGER trg_%1$s_updated_at BEFORE UPDATE ON %1$I FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t
        );
    END LOOP;
END
$do$;

-- ------------------------------------------------- Row Level Security ------
-- ENABLE (not FORCE): table owners (migrations, Supabase 'postgres') keep full access;
-- every other role, including the recommended non-owner application role, is filtered.
-- To subject the owner to the policies as well:  ALTER TABLE <t> FORCE ROW LEVEL SECURITY;
DO $do$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['users', 'campaigns', 'content_drafts', 'social_accounts', 'scheduled_tasks',
                         'agency_applications', 'page_comments']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I FOR ALL '
            'USING (app_bypass_rls() OR tenant_id = app_current_tenant()) '
            'WITH CHECK (app_bypass_rls() OR tenant_id = app_current_tenant())', t
        );
    END LOOP;
END
$do$;

-- tenants: a tenant sees only itself (id is the tenant key).
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON tenants;
CREATE POLICY tenant_isolation ON tenants FOR ALL
    USING (app_bypass_rls() OR id = app_current_tenant())
    WITH CHECK (app_bypass_rls() OR id = app_current_tenant());

-- ---------------------------------------------------------------- grants ---
-- Optional least-privilege application role (create it once, outside this script):
--   CREATE ROLE omniflow_app LOGIN PASSWORD '...' NOBYPASSRLS;
-- and then grant table access:
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omniflow_app') THEN
        GRANT USAGE ON SCHEMA public TO omniflow_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO omniflow_app;
    END IF;
END
$do$;
