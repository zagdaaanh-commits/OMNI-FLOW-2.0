-- OmniFlow persistent chat history (PostgreSQL / Supabase)
-- Idempotent migration. Apply after scripts/init_supabase.sql.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

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

CREATE TABLE IF NOT EXISTS conversations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   text        NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    user_id     text        NOT NULL,
    title       text        NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS messages (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id  uuid        NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    role             text        NOT NULL CHECK (role IN ('user', 'assistant')),
    content          text        NOT NULL CHECK (length(btrim(content)) > 0),
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_conversations_owner_updated
    ON conversations (tenant_id, user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_messages_conversation_created
    ON messages (conversation_id, created_at, id);

DROP TRIGGER IF EXISTS trg_conversations_updated_at ON conversations;
CREATE TRIGGER trg_conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

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
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'omniflow_app') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE conversations, messages TO omniflow_app';
    END IF;
END
$do$;
