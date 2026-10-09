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

-- Per-request chat owner: custom email sessions set app.user_id; direct Supabase
-- Data API calls fall back to auth.uid().
CREATE OR REPLACE FUNCTION app_current_user() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.user_id', true), '')
$$;

DO $do$
BEGIN
    IF to_regprocedure('auth.uid()') IS NOT NULL THEN
        EXECUTE $fn$
            CREATE OR REPLACE FUNCTION app_current_user() RETURNS text
            LANGUAGE sql STABLE AS $body$
                SELECT coalesce(
                    nullif(current_setting('app.user_id', true), ''),
                    auth.uid()::text
                )
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

-- ---------------------------------------------------------- conversations ---
CREATE TABLE IF NOT EXISTS conversations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id     text        NOT NULL,
    title       text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_conversations_owner_updated
    ON conversations (tenant_id, user_id, updated_at DESC);

-- --------------------------------------------------------------- messages ---
CREATE TABLE IF NOT EXISTS messages (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  uuid        NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role             text        NOT NULL CHECK (role IN ('user', 'assistant')),
    content          text        NOT NULL CHECK (length(btrim(content)) > 0),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_messages_conversation_created
    ON messages (conversation_id, created_at, id);

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
    business_license_path text,           -- object in the agency-documents bucket (supabase_storage_setup.sql)
    status        text        NOT NULL DEFAULT 'received',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE agency_applications ADD COLUMN IF NOT EXISTS business_license_path text;  -- databases created before it
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

-- ----------------------------------------------- email_verification_codes ---
-- One-time 6-digit codes for sign-up and password reset (app/verification.py), stored only as an
-- HMAC. One pending code per email + purpose. Not tenant-scoped: they exist before the account.
CREATE TABLE IF NOT EXISTS email_verification_codes (
    email       text        NOT NULL,
    purpose     text        NOT NULL,
    code_hash   text        NOT NULL,
    attempts    integer     NOT NULL DEFAULT 0,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (email, purpose),
    CONSTRAINT ck_email_verification_codes_purpose CHECK (purpose IN ('register', 'reset_password'))
);
-- Only the API (app.bypass_rls) may read or write codes; Supabase clients never see them.
ALTER TABLE email_verification_codes ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS api_only ON email_verification_codes;
CREATE POLICY api_only ON email_verification_codes FOR ALL
    USING (app_bypass_rls())
    WITH CHECK (app_bypass_rls());

-- --------------------------------------------- updated_at triggers (all) ---
DO $do$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['tenants', 'users', 'conversations', 'campaigns', 'content_drafts', 'social_accounts',
                         'scheduled_tasks', 'agency_applications', 'page_comments']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%1$s_updated_at ON %1$I', t);
        EXECUTE format(
            'CREATE TRIGGER trg_%1$s_updated_at BEFORE UPDATE ON %1$I FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t
        );
    END LOOP;
END
$do$;

-- A new message makes its conversation recent; the first user message supplies
-- a compact title even when the caller created the conversation with an empty title.
CREATE OR REPLACE FUNCTION touch_conversation_on_message() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE conversations
    SET title = CASE
            WHEN NEW.role = 'user' AND btrim(title) = ''
            THEN left(regexp_replace(btrim(NEW.content), '\s+', ' ', 'g'), 72)
            ELSE title
        END,
        updated_at = now()
    WHERE id = NEW.conversation_id;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_messages_touch_conversation ON messages;
CREATE TRIGGER trg_messages_touch_conversation
    AFTER INSERT ON messages
    FOR EACH ROW EXECUTE FUNCTION touch_conversation_on_message();

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

-- Chat history is user-owned. PUBLIC policies support both Supabase's
-- authenticated role and a custom non-owner application role; grants below keep
-- anonymous Data API callers out, and a missing app.user_id fails closed.
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS conversations_select_own ON conversations;
DROP POLICY IF EXISTS conversations_insert_own ON conversations;
DROP POLICY IF EXISTS conversations_update_own ON conversations;
DROP POLICY IF EXISTS conversations_delete_own ON conversations;
CREATE POLICY conversations_select_own ON conversations FOR SELECT TO PUBLIC
    USING (app_bypass_rls() OR user_id = app_current_user());
CREATE POLICY conversations_insert_own ON conversations FOR INSERT TO PUBLIC
    WITH CHECK (app_bypass_rls() OR user_id = app_current_user());
CREATE POLICY conversations_update_own ON conversations FOR UPDATE TO PUBLIC
    USING (app_bypass_rls() OR user_id = app_current_user())
    WITH CHECK (app_bypass_rls() OR user_id = app_current_user());
CREATE POLICY conversations_delete_own ON conversations FOR DELETE TO PUBLIC
    USING (app_bypass_rls() OR user_id = app_current_user());

DROP POLICY IF EXISTS messages_select_own ON messages;
DROP POLICY IF EXISTS messages_insert_own ON messages;
CREATE POLICY messages_select_own ON messages FOR SELECT TO PUBLIC
    USING (
        app_bypass_rls() OR EXISTS (
            SELECT 1 FROM conversations AS conversation
            WHERE conversation.id = messages.conversation_id
              AND conversation.user_id = app_current_user()
        )
    );
CREATE POLICY messages_insert_own ON messages FOR INSERT TO PUBLIC
    WITH CHECK (
        app_bypass_rls() OR EXISTS (
            SELECT 1 FROM conversations AS conversation
            WHERE conversation.id = messages.conversation_id
              AND conversation.user_id = app_current_user()
        )
    );

-- Supabase's October 2026 Data API change no longer auto-exposes new public
-- tables. Explicit least-privilege grants keep behavior deterministic.
DO $do$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        EXECUTE 'REVOKE ALL ON TABLE conversations, messages FROM anon';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        EXECUTE 'REVOKE ALL ON TABLE conversations, messages FROM authenticated';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE conversations TO authenticated';
        EXECUTE 'GRANT SELECT, INSERT ON TABLE messages TO authenticated';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'service_role') THEN
        EXECUTE 'GRANT ALL ON TABLE conversations, messages TO service_role';
    END IF;
END
$do$;

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
