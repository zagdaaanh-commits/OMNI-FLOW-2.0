-- =============================================================================
-- OmniFlow 2.0 - Supabase Storage: buckets + Row Level Security on storage.objects
--
-- Supabase only (needs the storage and auth schemas). Idempotent; run after
-- init_supabase.sql, and again whenever this file changes:
--     python scripts/migrate.py --sql scripts/supabase_storage_setup.sql
-- or paste it into the Supabase SQL editor.
--
-- Buckets
--   agency-documents  PRIVATE  business licenses (营业执照) attached to agency applications
--   ad-creatives      PUBLIC   campaign images that Meta / social platforms fetch by URL
-- Object names are '<workspace_id>/<uuid>.<ext>': the first folder is the workspace (tenant) id.
--
-- Who can do what
--   * The OmniFlow API (app/routers/upload.py) uses the service-role key, which bypasses RLS.
--     It builds every object name from the caller's verified workspace, so the policies below
--     are the second line of defence, for clients that use Storage directly with a Supabase JWT.
--   * A JWT carries its workspace in app_metadata.tenant_id (the same claim that
--     app_current_tenant() in init_supabase.sql reads) and its role in app_metadata.tenant_role.
--     app_metadata can only be set server-side, so users cannot change either claim.
--   * Uploads (both buckets): authenticated users, into their own workspace folder only.
--   * agency-documents reads: only the workspace's owner / admin. Clients can never update or
--     delete these files: an attached license stays as it was submitted.
--   * ad-creatives reads: anyone, through the public URL
--     /storage/v1/object/public/ad-creatives/<workspace_id>/<file>. That is what the bucket's
--     public flag grants. Listing the bucket through the API is limited to the caller's
--     workspace, so nobody can enumerate other workspaces' files or ids.
--   * The anon role gets no policy at all.
-- RLS is already enabled on storage.objects by Supabase. The table belongs to
-- supabase_storage_admin, so this script does not ALTER it; it only manages policies.
-- =============================================================================

DO $check$
BEGIN
    IF to_regclass('storage.buckets') IS NULL OR to_regclass('storage.objects') IS NULL
       OR to_regprocedure('auth.jwt()') IS NULL THEN
        RAISE EXCEPTION 'supabase_storage_setup.sql must run on a Supabase project (storage / auth schemas not found)';
    END IF;
END
$check$;

-- ---------------------------------------------------------------- buckets ---
-- Size and type limits are enforced by Storage itself, even for the service role.
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES
    ('agency-documents', 'agency-documents', false, 10485760, ARRAY['application/pdf', 'image/png', 'image/jpeg']),
    ('ad-creatives',     'ad-creatives',     true,  10485760, ARRAY['image/png', 'image/jpeg', 'image/webp'])
ON CONFLICT (id) DO UPDATE
SET public             = EXCLUDED.public,
    file_size_limit    = EXCLUDED.file_size_limit,
    allowed_mime_types = EXCLUDED.allowed_mime_types;

-- ---------------------------------------------------------------- helpers ---
-- Workspace of the calling JWT (NULL for anon / service role without the claim).
CREATE OR REPLACE FUNCTION public.storage_jwt_workspace() RETURNS text
LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT nullif(auth.jwt() -> 'app_metadata' ->> 'tenant_id', '')
$$;

-- True when the calling JWT is the workspace owner or an admin.
CREATE OR REPLACE FUNCTION public.storage_jwt_is_workspace_admin() RETURNS boolean
LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT coalesce(auth.jwt() -> 'app_metadata' ->> 'tenant_role', '') IN ('owner', 'admin')
$$;

-- True when an object name is exactly '<caller's workspace>/<file>' (no deeper folders).
CREATE OR REPLACE FUNCTION public.storage_object_in_jwt_workspace(object_name text) RETURNS boolean
LANGUAGE sql STABLE SET search_path = '' AS $$
    SELECT public.storage_jwt_workspace() IS NOT NULL
       AND array_length(storage.foldername(object_name), 1) = 1
       AND (storage.foldername(object_name))[1] = public.storage_jwt_workspace()
$$;

-- -------------------------------------------------------- agency-documents ---
DROP POLICY IF EXISTS "agency_documents_insert_own_workspace" ON storage.objects;
CREATE POLICY "agency_documents_insert_own_workspace" ON storage.objects
    FOR INSERT TO authenticated
    WITH CHECK (bucket_id = 'agency-documents' AND public.storage_object_in_jwt_workspace(name));

DROP POLICY IF EXISTS "agency_documents_select_workspace_admin" ON storage.objects;
CREATE POLICY "agency_documents_select_workspace_admin" ON storage.objects
    FOR SELECT TO authenticated
    USING (
        bucket_id = 'agency-documents'
        AND public.storage_object_in_jwt_workspace(name)
        AND public.storage_jwt_is_workspace_admin()
    );
-- No UPDATE / DELETE policies: clients cannot change or remove submitted documents.

-- ------------------------------------------------------------ ad-creatives ---
DROP POLICY IF EXISTS "ad_creatives_insert_own_workspace" ON storage.objects;
CREATE POLICY "ad_creatives_insert_own_workspace" ON storage.objects
    FOR INSERT TO authenticated
    WITH CHECK (bucket_id = 'ad-creatives' AND public.storage_object_in_jwt_workspace(name));

-- Listing / API reads within the own workspace (public URLs need no policy).
DROP POLICY IF EXISTS "ad_creatives_select_own_workspace" ON storage.objects;
CREATE POLICY "ad_creatives_select_own_workspace" ON storage.objects
    FOR SELECT TO authenticated
    USING (bucket_id = 'ad-creatives' AND public.storage_object_in_jwt_workspace(name));

DROP POLICY IF EXISTS "ad_creatives_update_own_workspace" ON storage.objects;
CREATE POLICY "ad_creatives_update_own_workspace" ON storage.objects
    FOR UPDATE TO authenticated
    USING (bucket_id = 'ad-creatives' AND public.storage_object_in_jwt_workspace(name))
    WITH CHECK (bucket_id = 'ad-creatives' AND public.storage_object_in_jwt_workspace(name));

DROP POLICY IF EXISTS "ad_creatives_delete_own_workspace" ON storage.objects;
CREATE POLICY "ad_creatives_delete_own_workspace" ON storage.objects
    FOR DELETE TO authenticated
    USING (bucket_id = 'ad-creatives' AND public.storage_object_in_jwt_workspace(name));
