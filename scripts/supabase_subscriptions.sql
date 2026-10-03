-- =============================================================================
-- OmniFlow 2.0 - subscriptions, AI usage, upgrade requests and payments (PostgreSQL / Supabase)
--
-- Idempotent. Runs after init_supabase.sql (it reuses tenants, set_updated_at(),
-- app_bypass_rls() and app_current_tenant()). `python scripts/migrate.py` applies both.
--
-- Workspaces are the `tenants` table, so the workspace key is `tenant_id text`
-- (ids are UUID strings, plus the literal 'default' house account).
--
-- Plans (prices and limits live in app/plans.py): 'pro' = Pro Growth, 'agency' = Agency VIP.
-- There is no free tier: new workspaces get a Pro 'trial'; when current_period_end passes the
-- workspace is treated as expired until a plan is paid for: Stripe Checkout (billing_payments,
-- activated by the webhook) or a manual payment the operator activates (scripts/set_plan.py).
-- current_period_end NULL = no end date (the 'default' house account).
--
-- Who may write: only the OmniFlow API. Its transactions set 'app.tenant_id' (or
-- 'app.bypass_rls' for operator tooling); Supabase clients cannot set those settings, so with a
-- JWT a workspace can read its own rows but never change its plan or usage.
-- =============================================================================

SELECT set_config('app.bypass_rls', 'on', true);

-- Tenant set by the API server for this transaction (never the JWT fallback).
CREATE OR REPLACE FUNCTION app_server_tenant() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT nullif(current_setting('app.tenant_id', true), '')
$$;

-- ----------------------------------------------------------- subscriptions ---
CREATE TABLE IF NOT EXISTS subscriptions (
    id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id           text        NOT NULL UNIQUE REFERENCES tenants (id) ON DELETE CASCADE,
    plan                text        NOT NULL DEFAULT 'pro',
    billing_cycle       text        NOT NULL DEFAULT 'monthly',
    status              text        NOT NULL DEFAULT 'trial',
    current_period_end  timestamptz DEFAULT now() + interval '30 days',
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_subscriptions_plan CHECK (plan IN ('pro', 'agency')),
    CONSTRAINT ck_subscriptions_cycle CHECK (billing_cycle IN ('monthly', 'annual')),
    CONSTRAINT ck_subscriptions_status CHECK (status IN ('active', 'trial', 'expired'))
);

-- ---------------------------------------------------------- usage_tracking ---
-- AI runs per rolling 30-day cycle (also on annual plans).
CREATE TABLE IF NOT EXISTS usage_tracking (
    tenant_id      text        PRIMARY KEY REFERENCES tenants (id) ON DELETE CASCADE,
    ai_runs_count  integer     NOT NULL DEFAULT 0,
    cycle_start    timestamptz NOT NULL DEFAULT now(),
    cycle_end      timestamptz NOT NULL DEFAULT now() + interval '30 days',
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_usage_tracking_runs CHECK (ai_runs_count >= 0)
);

-- -------------------------------------------------------- upgrade_requests ---
-- "Upgrade" clicks from the pricing modal; the operator follows up for payment and activates.
CREATE TABLE IF NOT EXISTS upgrade_requests (
    id             text        PRIMARY KEY,
    tenant_id      text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id        text,
    plan           text        NOT NULL,
    billing_cycle  text        NOT NULL,
    status         text        NOT NULL DEFAULT 'pending',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_upgrade_requests_plan CHECK (plan IN ('pro', 'agency')),
    CONSTRAINT ck_upgrade_requests_cycle CHECK (billing_cycle IN ('monthly', 'annual')),
    CONSTRAINT ck_upgrade_requests_status CHECK (status IN ('pending', 'fulfilled', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS ix_upgrade_requests_tenant ON upgrade_requests (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_upgrade_requests_pending ON upgrade_requests (created_at) WHERE status = 'pending';

-- -------------------------------------------------------- billing_payments ---
-- One row per online payment (id = Stripe Checkout Session id), inserted in the same transaction
-- that extends the subscription, so a webhook retry can never apply a payment twice.
-- status 'paid' = applied to the subscription; 'review' = kept for the operator (amount mismatch,
-- house account) and not applied.
CREATE TABLE IF NOT EXISTS billing_payments (
    id             text        PRIMARY KEY,
    tenant_id      text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    provider       text        NOT NULL DEFAULT 'stripe',
    plan           text        NOT NULL,
    billing_cycle  text        NOT NULL,
    amount         integer     NOT NULL,          -- minor units (fen)
    currency       text        NOT NULL,
    status         text        NOT NULL DEFAULT 'paid',
    reference      text,                          -- Stripe PaymentIntent id
    period_end     timestamptz,                   -- subscription end after this payment
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_billing_payments_plan CHECK (plan IN ('pro', 'agency')),
    CONSTRAINT ck_billing_payments_cycle CHECK (billing_cycle IN ('monthly', 'annual')),
    CONSTRAINT ck_billing_payments_status CHECK (status IN ('paid', 'review'))
);
CREATE INDEX IF NOT EXISTS ix_billing_payments_tenant ON billing_payments (tenant_id, created_at DESC);

-- -------------------------------------------------------- increment_ai_runs ---
-- Counts one AI run and returns the new count. When the cycle has ended it restarts at 1 with a
-- fresh 30-day window. With p_limit, a cycle that already has p_limit runs is left unchanged and
-- NULL is returned; the row lock makes this exact under concurrency.
-- (p_limit NULL = unconditional increment, e.g. Agency VIP.)
CREATE OR REPLACE FUNCTION increment_ai_runs(p_tenant_id text, p_limit integer DEFAULT NULL)
RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    new_count integer;
BEGIN
    INSERT INTO usage_tracking (tenant_id) VALUES (p_tenant_id) ON CONFLICT (tenant_id) DO NOTHING;
    UPDATE usage_tracking
       SET ai_runs_count = CASE WHEN now() > cycle_end THEN 1 ELSE ai_runs_count + 1 END,
           cycle_start   = CASE WHEN now() > cycle_end THEN now() ELSE cycle_start END,
           cycle_end     = CASE WHEN now() > cycle_end THEN now() + interval '30 days' ELSE cycle_end END
     WHERE tenant_id = p_tenant_id
       AND (p_limit IS NULL OR now() > cycle_end OR ai_runs_count < p_limit)
    RETURNING ai_runs_count INTO new_count;
    RETURN new_count;
END;
$$;
REVOKE ALL ON FUNCTION increment_ai_runs(text, integer) FROM PUBLIC;

-- --------------------------------------------------------- updated_at ---
DO $do$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['subscriptions', 'usage_tracking', 'upgrade_requests', 'billing_payments']
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS trg_%1$s_updated_at ON %1$I', t);
        EXECUTE format(
            'CREATE TRIGGER trg_%1$s_updated_at BEFORE UPDATE ON %1$I FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t
        );
    END LOOP;
END
$do$;

-- --------------------------------------------------- Row Level Security ---
-- Read: the workspace itself (server session or a Supabase JWT with app_metadata.tenant_id).
-- Write: only the API server for that workspace, or operator tooling (app.bypass_rls).
DO $do$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['subscriptions', 'usage_tracking', 'upgrade_requests', 'billing_payments']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS workspace_read ON %I', t);
        EXECUTE format(
            'CREATE POLICY workspace_read ON %I FOR SELECT '
            'USING (app_bypass_rls() OR tenant_id = app_current_tenant())', t
        );
        EXECUTE format('DROP POLICY IF EXISTS server_write ON %I', t);
        EXECUTE format(
            'CREATE POLICY server_write ON %I FOR ALL '
            'USING (app_bypass_rls() OR tenant_id = app_server_tenant()) '
            'WITH CHECK (app_bypass_rls() OR tenant_id = app_server_tenant())', t
        );
    END LOOP;
END
$do$;

-- Supabase API roles: read only (RLS still limits them to their own workspace).
DO $do$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON subscriptions, usage_tracking, upgrade_requests, billing_payments FROM %I', r);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE SELECT ON subscriptions, usage_tracking, upgrade_requests, billing_payments FROM anon;
    END IF;
    -- The optional least-privilege application role from init_supabase.sql.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omniflow_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON subscriptions, usage_tracking, upgrade_requests, billing_payments TO omniflow_app;
        GRANT EXECUTE ON FUNCTION increment_ai_runs(text, integer) TO omniflow_app;
    END IF;
END
$do$;

-- ------------------------------------------------------------------ seed ---
-- The default workspace is the operator's house account: Agency VIP without an end date.
INSERT INTO subscriptions (tenant_id, plan, billing_cycle, status, current_period_end)
VALUES ('default', 'agency', 'annual', 'active', NULL)
ON CONFLICT (tenant_id) DO NOTHING;

-- Workspaces created before billing existed start the standard 7-day Pro trial now
-- (SUBSCRIPTION_TRIAL_DAYS in the app; the API also does this lazily on first use).
INSERT INTO subscriptions (tenant_id, plan, billing_cycle, status, current_period_end)
SELECT id, 'pro', 'monthly', 'trial', now() + interval '7 days'
FROM tenants
WHERE id <> 'default'
ON CONFLICT (tenant_id) DO NOTHING;
