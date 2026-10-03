"""Supabase Storage uploads (POST /api/upload/document, /api/upload/creative), the business-license
path on agency applications, and the storage.objects policies in scripts/supabase_storage_setup.sql.

Uploads go through the real supabase-py client; only its HTTP transport is replaced with an
httpx.MockTransport, so the requests sent to Supabase are those the library really builds.
The PostgreSQL tests at the end run only when TEST_DATABASE_URL is set (as in CI).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

import app.object_storage as object_storage
from app.main import app, store

client = TestClient(app)

ROOT = Path(__file__).resolve().parent.parent
SUPABASE_URL = "https://proj-test.supabase.co"
SERVICE_KEY = "service-role-test-key-0123456789"
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
APPLICATION = {
    "company_name": "Shenzhen Example Trading Co., Ltd.",
    "credit_code": "91330100799655058B",
    "store_url": "https://shop.example.com",
    "contact": "wechat: example",
}


class StorageStub:
    """Stands in for the Supabase Storage REST API."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.upload_reply = None
        self.sign_reply = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/storage/v1/object/sign/"):
            if self.sign_reply:
                return self.sign_reply(request)
            return httpx.Response(200, json={"signedURL": f"/object/sign/{path[len('/storage/v1/object/sign/'):]}?token=signed-token"})
        if path.startswith("/storage/v1/object/"):
            if self.upload_reply:
                return self.upload_reply(request)
            return httpx.Response(200, json={"Key": path[len("/storage/v1/object/"):], "Id": str(uuid4())})
        return httpx.Response(404, json={"statusCode": "404", "error": "not_found", "message": "unexpected call"})

    @property
    def uploads(self) -> list[httpx.Request]:
        return [r for r in self.requests if not r.url.path.startswith("/storage/v1/object/sign/")]

    @property
    def signs(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith("/storage/v1/object/sign/")]


@pytest.fixture
def storage_stub(monkeypatch):
    stub = StorageStub()
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", SERVICE_KEY)
    monkeypatch.setattr(
        object_storage,
        "build_httpx_client",
        lambda timeout=None, **kw: httpx.Client(transport=httpx.MockTransport(stub.handler)),
    )
    object_storage.reset_object_storage()
    yield stub
    object_storage.reset_object_storage()


def _register():
    res = client.post(
        "/auth/register",
        json={"email": f"up_{uuid4().hex[:8]}@example.com", "password": "Passw0rd!", "full_name": "Uploader"},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    return body["user"]["tenant_id"], {"Authorization": f"Bearer {body['token']}"}


def _upload(headers, data, filename="license.pdf", content_type="application/pdf", kind="document"):
    return client.post(f"/api/upload/{kind}", files={"file": (filename, data, content_type)}, headers=headers)


# --------------------------------------------------------------------------- documents
def test_license_is_stored_in_the_workspace_folder_and_signed(storage_stub):
    tenant_id, headers = _register()
    res = _upload(headers, PDF, filename="营业执照.pdf")
    assert res.status_code == 200, res.text
    body = res.json()

    path = body["path"]
    assert re.fullmatch(rf"{re.escape(tenant_id)}/{UUID}\.pdf", path)
    assert body["bucket"] == "agency-documents"
    assert body["filename"] == "营业执照.pdf"
    assert body["content_type"] == "application/pdf" and body["size"] == len(PDF)
    assert body["signed_url"] == f"{SUPABASE_URL}/storage/v1/object/sign/agency-documents/{path}?token=signed-token"
    assert body["expires_in"] == 600

    (upload,) = storage_stub.uploads
    assert upload.method == "POST"
    assert str(upload.url) == f"{SUPABASE_URL}/storage/v1/object/agency-documents/{path}"
    assert upload.headers["authorization"] == f"Bearer {SERVICE_KEY}"
    assert upload.headers["x-upsert"] == "false"  # never overwrite an existing object
    assert PDF in upload.content and b"application/pdf" in upload.content

    (sign,) = storage_stub.signs
    assert json.loads(sign.content) == {"expiresIn": "600"}
    assert SERVICE_KEY not in res.text


@pytest.mark.parametrize(
    "filename, data, ext, content_type",
    [
        ("scan.png", PNG, "png", "image/png"),
        ("scan.JPG", JPG, "jpg", "image/jpeg"),
        ("scan.jpeg", JPG, "jpg", "image/jpeg"),
        ("scan", PNG, "png", "image/png"),  # no extension: the bytes decide
    ],
)
def test_png_and_jpg_licenses_are_accepted(storage_stub, filename, data, ext, content_type):
    tenant_id, headers = _register()
    res = _upload(headers, data, filename=filename, content_type="application/octet-stream")
    assert res.status_code == 200, res.text
    assert res.json()["path"].endswith("." + ext) and res.json()["content_type"] == content_type
    assert content_type.encode() in storage_stub.uploads[0].content


@pytest.mark.parametrize(
    "filename, data, content_type",
    [
        ("animation.gif", b"GIF89a" + b"\x00" * 32, "image/gif"),
        ("license.pdf", b"<html><script>alert(1)</script></html>", "application/pdf"),  # declared type is ignored
        ("license.exe", b"MZ\x90\x00" + b"\x00" * 32, "application/octet-stream"),
        ("license.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', "image/svg+xml"),
        ("photo.webp", WEBP, "image/webp"),  # fine for creatives, not for licenses
        ("license.html", PNG, "image/png"),  # extension disagrees with the bytes
        ("license.png", PDF, "image/png"),
    ],
)
def test_other_file_types_are_refused(storage_stub, filename, data, content_type):
    _, headers = _register()
    res = _upload(headers, data, filename=filename, content_type=content_type)
    assert res.status_code == 415
    assert res.json()["detail"] == "Only PDF, PNG or JPG files are allowed"
    assert storage_stub.requests == []


def test_files_over_10_mb_are_refused(storage_stub):
    _, headers = _register()
    too_big = PDF + b"0" * (object_storage.MAX_UPLOAD_BYTES - len(PDF) + 1)
    res = _upload(headers, too_big)
    assert res.status_code == 413
    assert storage_stub.requests == []


def test_a_file_of_exactly_10_mb_is_accepted(storage_stub):
    _, headers = _register()
    res = _upload(headers, PDF + b"0" * (object_storage.MAX_UPLOAD_BYTES - len(PDF)))
    assert res.status_code == 200, res.text
    assert res.json()["size"] == object_storage.MAX_UPLOAD_BYTES


def test_empty_missing_or_malformed_uploads_are_refused(storage_stub):
    _, headers = _register()
    assert _upload(headers, b"").status_code == 400
    assert client.post("/api/upload/document", files={"other": ("a.pdf", PDF, "application/pdf")}, headers=headers).status_code == 400
    assert client.post("/api/upload/document", json={"file": "a.pdf"}, headers=headers).status_code == 400
    two = [("file", ("a.pdf", PDF, "application/pdf")), ("file", ("b.pdf", PDF, "application/pdf"))]
    assert client.post("/api/upload/document", files=two, headers=headers).status_code == 400
    assert storage_stub.requests == []


@pytest.mark.parametrize(
    "filename, shown",
    [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\Windows\\System32\\evil.pdf", "evil.pdf"),
        ("/var/www/abs.pdf", "abs.pdf"),
        ("C:\\Users\\merchant\\营业执照 扫描件.pdf", "营业执照 扫描件.pdf"),
        ("a<b>c:d|e?f*.pdf", "abcdef.pdf"),
        ("..pdf", "pdf"),
    ],
)
def test_client_filenames_never_reach_the_storage_path(storage_stub, filename, shown):
    tenant_id, headers = _register()
    res = _upload(headers, PDF, filename=filename)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["filename"] == shown
    assert re.fullmatch(rf"{re.escape(tenant_id)}/{UUID}\.pdf", body["path"])
    assert str(storage_stub.uploads[0].url) == f"{SUPABASE_URL}/storage/v1/object/agency-documents/{body['path']}"


def test_safe_filename_strips_control_and_reserved_characters():
    # httpx percent-encodes these inside multipart headers, so they are checked directly.
    assert object_storage.safe_filename("li\x00cen\x07se.pdf") == "license.pdf"
    assert object_storage.safe_filename('a"b\tc\nd.pdf') == "abcd.pdf"


def test_safe_filename_limits_length_and_keeps_the_extension():
    long_name = "营" * 300 + ".pdf"
    shown = object_storage.safe_filename(long_name)
    assert len(shown) == 120 and shown.endswith(".pdf")
    assert object_storage.safe_filename("", default="upload.pdf") == "upload.pdf"
    assert object_storage.safe_filename("  ..  ", default="upload.pdf") == "upload.pdf"


def test_uploads_require_a_signed_in_user(storage_stub):
    assert _upload({}, PDF).status_code == 401
    assert _upload({"Authorization": "Bearer omt_not-a-real-token"}, PDF).status_code == 401
    assert storage_stub.requests == []


def test_each_workspace_writes_only_to_its_own_folder(storage_stub):
    tenant_a, a = _register()
    tenant_b, b = _register()
    assert _upload(a, PDF).json()["path"].startswith(tenant_a + "/")
    assert _upload(b, PDF).json()["path"].startswith(tenant_b + "/")
    folders = {r.url.path.split("/")[5] for r in storage_stub.uploads}  # /storage/v1/object/<bucket>/<folder>/...
    assert folders == {tenant_a, tenant_b}


def test_uploads_answer_503_when_storage_is_not_configured():
    _, headers = _register()
    object_storage.reset_object_storage()
    res = _upload(headers, PDF)
    assert res.status_code == 503 and "not configured" in res.json()["detail"]
    assert client.get("/config/public").json()["document_upload_enabled"] is False


def test_public_config_reports_uploads_when_configured(storage_stub):
    assert client.get("/config/public").json()["document_upload_enabled"] is True


def test_storage_errors_are_reported_without_secrets(storage_stub):
    _, headers = _register()
    storage_stub.upload_reply = lambda request: httpx.Response(
        400, json={"statusCode": "404", "error": "Bucket not found", "message": "Bucket not found"}
    )
    res = _upload(headers, PDF)
    assert res.status_code == 502 and res.json()["detail"] == "Could not store the file. Please try again."
    assert SERVICE_KEY not in res.text

    def unreachable(request):
        raise httpx.ConnectError("connection refused", request=request)

    storage_stub.upload_reply = unreachable
    assert _upload(headers, PDF).status_code == 502


def test_a_failed_signature_still_returns_the_stored_path(storage_stub):
    tenant_id, headers = _register()
    storage_stub.sign_reply = lambda request: httpx.Response(500, json={"statusCode": "500", "error": "internal", "message": "boom"})
    res = _upload(headers, PDF)
    assert res.status_code == 200
    assert res.json()["path"].startswith(tenant_id + "/") and res.json()["signed_url"] is None


# --------------------------------------------------------------------------- creatives
@pytest.mark.parametrize("filename, data, ext", [("hero.png", PNG, "png"), ("hero.jpg", JPG, "jpg"), ("hero.webp", WEBP, "webp")])
def test_creatives_go_to_the_public_bucket(storage_stub, filename, data, ext):
    tenant_id, headers = _register()
    res = _upload(headers, data, filename=filename, content_type="image/" + ext, kind="creative")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["bucket"] == "ad-creatives"
    assert re.fullmatch(rf"{re.escape(tenant_id)}/{UUID}\.{ext}", body["path"])
    assert body["public_url"] == f"{SUPABASE_URL}/storage/v1/object/public/ad-creatives/{body['path']}"
    assert "signed_url" not in body
    assert storage_stub.signs == []


def test_creatives_must_be_images(storage_stub):
    _, headers = _register()
    res = _upload(headers, PDF, kind="creative")
    assert res.status_code == 415 and res.json()["detail"] == "Only PNG, JPG or WebP files are allowed"
    assert storage_stub.requests == []


# ------------------------------------------------------------- agency application link
def test_application_keeps_the_uploaded_license_path(storage_stub):
    tenant_id, headers = _register()
    path = _upload(headers, PDF).json()["path"]
    res = client.post("/api/agency/apply", json={**APPLICATION, "business_license_path": path}, headers=headers)
    assert res.status_code == 200, res.text
    (saved,) = store.list_agency_applications(tenant_id=tenant_id)
    assert saved["business_license_path"] == path


def test_application_without_a_license_still_works():
    tenant_id, headers = _register()
    for value in (None, "", "   "):
        assert client.post("/api/agency/apply", json={**APPLICATION, "business_license_path": value}, headers=headers).status_code == 200
    assert all(a["business_license_path"] is None for a in store.list_agency_applications(tenant_id=tenant_id))


def test_application_cannot_attach_another_workspaces_file(storage_stub):
    tenant_a, a = _register()
    tenant_b, b = _register()
    path_a = _upload(a, PDF).json()["path"]
    res = client.post("/api/agency/apply", json={**APPLICATION, "business_license_path": path_a}, headers=b)
    assert res.status_code == 422
    assert res.json()["detail"][0]["loc"] == ["body", "business_license_path"]
    assert store.list_agency_applications(tenant_id=tenant_b) == []


@pytest.mark.parametrize(
    "path",
    [
        "../../etc/passwd",
        "{tenant}/../../other/x.pdf",
        "{tenant}/not-a-uuid.pdf",
        "{tenant}/" + "0" * 8 + "-0000-4000-8000-" + "0" * 12 + ".exe",
        "{tenant}/" + "0" * 8 + "-0000-4000-8000-" + "0" * 12 + ".webp",  # creatives are not licenses
        "{tenant}/sub/" + "0" * 8 + "-0000-4000-8000-" + "0" * 12 + ".pdf",
        "x" * 300,
    ],
)
def test_application_rejects_malformed_license_paths(path):
    tenant_id, headers = _register()
    res = client.post(
        "/api/agency/apply", json={**APPLICATION, "business_license_path": path.format(tenant=tenant_id)}, headers=headers
    )
    assert res.status_code == 422
    assert store.list_agency_applications(tenant_id=tenant_id) == []


def test_existing_sqlite_databases_gain_the_license_column(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE agency_applications (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id TEXT, "
            "company_name TEXT NOT NULL, credit_code TEXT NOT NULL, store_url TEXT NOT NULL, contact TEXT NOT NULL, "
            "remarks TEXT, status TEXT NOT NULL DEFAULT 'received', created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
    from db.sqlite_store import SQLiteStore

    migrated = SQLiteStore(str(db))
    saved = migrated.save_agency_application({**APPLICATION, "business_license_path": "default/x.pdf"})
    assert saved["business_license_path"] == "default/x.pdf"


# ------------------------------------------------------------------------- PostgreSQL
PG_URL = os.environ.get("TEST_DATABASE_URL", "")
pg_only = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL not set (no PostgreSQL available)")
INIT_SQL = ROOT / "scripts" / "init_supabase.sql"
STORAGE_SQL = ROOT / "scripts" / "supabase_storage_setup.sql"

# Minimal stand-ins for the parts of Supabase the storage script relies on.
# storage.foldername() is Supabase's own definition.
SUPABASE_STUB = r"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN CREATE ROLE anon NOLOGIN; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN CREATE ROLE authenticated NOLOGIN; END IF;
END
$$;
CREATE SCHEMA auth;
CREATE FUNCTION auth.jwt() RETURNS jsonb LANGUAGE sql STABLE AS $$
    SELECT coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb
$$;
CREATE SCHEMA storage;
CREATE TABLE storage.buckets (
    id text PRIMARY KEY, name text NOT NULL, public boolean DEFAULT false,
    file_size_limit bigint, allowed_mime_types text[]
);
CREATE TABLE storage.objects (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), bucket_id text REFERENCES storage.buckets (id), name text
);
CREATE FUNCTION storage.foldername(name text) RETURNS text[] LANGUAGE plpgsql AS $$
DECLARE _parts text[];
BEGIN
    SELECT string_to_array(name, '/') INTO _parts;
    RETURN _parts[1:array_length(_parts, 1) - 1];
END
$$;
ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
GRANT USAGE ON SCHEMA auth, storage TO anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON storage.objects TO anon, authenticated;
GRANT SELECT ON storage.buckets TO anon, authenticated;
"""


@pg_only
def test_postgres_store_keeps_the_license_path():
    import psycopg

    from db.postgres_store import PostgresStore

    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute(INIT_SQL.read_text(encoding="utf-8"))
        # A database created before the column existed is upgraded by the same script.
        conn.execute("ALTER TABLE agency_applications DROP COLUMN business_license_path")
        conn.execute(INIT_SQL.read_text(encoding="utf-8"))

    pg_store = PostgresStore(PG_URL, min_size=1, max_size=2)
    try:
        tenant = pg_store.create_tenant(f"License {uuid4().hex[:6]}")
        path = f"{tenant['id']}/{uuid4()}.pdf"
        saved = pg_store.save_agency_application({**APPLICATION, "business_license_path": path}, tenant_id=tenant["id"])
        assert saved["business_license_path"] == path
        assert pg_store.list_agency_applications(tenant_id=tenant["id"])[0]["business_license_path"] == path
    finally:
        pg_store.close()


@pytest.fixture
def supabase_db():
    """A transaction with stub auth/storage schemas plus the storage script; always rolled back."""
    import psycopg

    conn = psycopg.connect(PG_URL)
    try:
        if conn.execute("SELECT to_regnamespace('storage') IS NOT NULL").fetchone()[0]:
            pytest.skip("this database already has a storage schema (a real Supabase project?)")
        try:
            conn.execute(SUPABASE_STUB)
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("creating the stub roles needs CREATEROLE")
        sql = STORAGE_SQL.read_text(encoding="utf-8")
        conn.execute(sql)
        conn.execute(sql)  # idempotent
        conn.execute(
            "INSERT INTO storage.objects (bucket_id, name) VALUES "
            "('agency-documents', 'ws1/a.pdf'), ('agency-documents', 'ws2/b.pdf'), "
            "('ad-creatives', 'ws1/c.png'), ('ad-creatives', 'ws2/d.png')"
        )
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _as(conn, claims, sql, params=None, role="authenticated"):
    """Run one statement as a Supabase API role with the given JWT claims."""
    try:
        with conn.transaction():  # savepoint: a policy violation does not end the outer transaction
            conn.execute(f"SET LOCAL ROLE {role}")
            conn.execute("SELECT set_config('request.jwt.claims', %s, true)", (json.dumps(claims),))
            cur = conn.execute(sql, params)
            return {r[0] for r in cur.fetchall()} if cur.description else cur.rowcount
    finally:
        conn.execute("RESET ROLE")


def _claims(workspace=None, role="owner"):
    meta = {"tenant_id": workspace, "tenant_role": role} if workspace else {}
    return {"role": "authenticated", "app_metadata": meta}


INSERT = "INSERT INTO storage.objects (bucket_id, name) VALUES (%s, %s)"
NAMES = "SELECT name FROM storage.objects WHERE bucket_id = %s"


@pg_only
def test_storage_script_refuses_plain_postgres():
    import psycopg

    with psycopg.connect(PG_URL) as conn:
        if conn.execute("SELECT to_regnamespace('storage') IS NOT NULL").fetchone()[0]:
            pytest.skip("this database has a storage schema")
        with pytest.raises(psycopg.errors.RaiseException, match="must run on a Supabase project"):
            conn.execute(STORAGE_SQL.read_text(encoding="utf-8"))
        conn.rollback()


@pg_only
def test_storage_buckets_are_created_with_limits(supabase_db):
    rows = supabase_db.execute("SELECT id, public, file_size_limit, allowed_mime_types FROM storage.buckets ORDER BY id").fetchall()
    assert rows == [
        ("ad-creatives", True, 10485760, ["image/png", "image/jpeg", "image/webp"]),
        ("agency-documents", False, 10485760, ["application/pdf", "image/png", "image/jpeg"]),
    ]


@pg_only
def test_agency_documents_policies(supabase_db):
    import psycopg

    db = supabase_db
    owner, member, other_admin = _claims("ws1"), _claims("ws1", "member"), _claims("ws2", "admin")

    assert _as(db, owner, INSERT, ("agency-documents", "ws1/new.pdf")) == 1
    assert _as(db, member, INSERT, ("agency-documents", "ws1/by-member.pdf")) == 1  # any member may upload
    for bad in ("ws2/x.pdf", "ws1/sub/x.pdf", "x.pdf"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _as(db, owner, INSERT, ("agency-documents", bad))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _as(db, _claims(), INSERT, ("agency-documents", "ws1/x.pdf"))  # no workspace claim

    # Reading is limited to the workspace owner / admin.
    assert _as(db, owner, NAMES, ("agency-documents",)) == {"ws1/a.pdf", "ws1/new.pdf", "ws1/by-member.pdf"}
    assert _as(db, other_admin, NAMES, ("agency-documents",)) == {"ws2/b.pdf"}
    assert _as(db, member, NAMES, ("agency-documents",)) == set()

    # Submitted documents cannot be changed or removed by clients.
    assert _as(db, owner, "UPDATE storage.objects SET name = 'ws1/renamed.pdf' WHERE bucket_id = 'agency-documents'") == 0
    assert _as(db, owner, "DELETE FROM storage.objects WHERE bucket_id = 'agency-documents'") == 0


@pg_only
def test_ad_creatives_policies(supabase_db):
    import psycopg

    db = supabase_db
    member = _claims("ws1", "member")

    assert _as(db, member, INSERT, ("ad-creatives", "ws1/new.png")) == 1
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _as(db, member, INSERT, ("ad-creatives", "ws2/x.png"))
    # Listing shows only the own workspace (files are public by URL, not enumerable).
    assert _as(db, member, NAMES, ("ad-creatives",)) == {"ws1/c.png", "ws1/new.png"}
    assert _as(db, member, "DELETE FROM storage.objects WHERE name = 'ws2/d.png'") == 0
    assert _as(db, member, "DELETE FROM storage.objects WHERE name = 'ws1/c.png'") == 1


@pg_only
def test_anonymous_callers_get_nothing(supabase_db):
    import psycopg

    db = supabase_db
    anon = {"role": "anon"}
    assert _as(db, anon, "SELECT name FROM storage.objects", role="anon") == set()
    for bucket in ("agency-documents", "ad-creatives"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            _as(db, anon, INSERT, (bucket, "ws1/x.png"), role="anon")
