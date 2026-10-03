# OmniFlow deployment runbook (Hong Kong cloud)

Audience: the DevOps engineer deploying OmniFlow for a merchant on an Alibaba Cloud HK or Tencent Cloud HK Ubuntu server.

## Architecture

```
Internet ──443──> nginx ──> app (gunicorn + uvicorn workers, x N) ──> Supabase / PostgreSQL
                     │                    └──> Meta Graph API, Zhipu GLM (direct outbound)
                     └── /.well-known ──> certbot (renews every 12h)
worker (python -m app.worker) ──> polls the DB for due posts, publishes them
redis  ──> scheduler leader lock (no data loss if unavailable)
```

Scheduled posts live in the database, so they survive restarts of any container.

## Prerequisites

- Ubuntu 22.04 or 24.04 VM in Hong Kong, 2 vCPU and 4 GB RAM or more.
- DNS A record for your domain pointing at the VM.
- Security group open for TCP 80 and 443 only (plus SSH from your IP).
- A Supabase (or other PostgreSQL) project, and its connection string with `?sslmode=require`.
- A Meta app (App ID and secret) if merchants will connect Facebook pages via OAuth.

## First deploy

```bash
git clone <repo> && cd <repo>
cp .env.example .env && nano .env      # set DOMAIN, LETSENCRYPT_EMAIL, APP_SECRET_KEY, REDIS_PASSWORD, DATABASE_URL, OPENAI_API_KEY, META_*
chmod +x scripts/deploy_hk.sh
./scripts/deploy_hk.sh
```

The script installs Docker if needed, builds the image, applies the database migration, obtains a Let's Encrypt certificate on first run, and starts everything. Use `--dry-run` to preview.

Keep `OUTBOUND_PROXY_URL` empty on the HK server so traffic goes out directly.

## Routine deploys and rollback

Run `./scripts/deploy_hk.sh` again. It pulls with `--ff-only`, rebuilds, migrates, starts new `app` replicas next to the old ones, waits until they are healthy, then retires the old ones. If the new replicas never become healthy they are removed, the old ones keep serving, and the script exits non-zero.

To roll back code after a successful deploy: `git checkout <previous-commit> && ./scripts/deploy_hk.sh --skip-pull`.

Flags: `--skip-pull`, `--skip-build`, `--no-cert`, `--dry-run`.

## Verify

```bash
curl -s https://$DOMAIN/health
curl -s https://$DOMAIN/health/network      # reachability and latency to graph.facebook.com and open.bigmodel.cn
curl -s "https://$DOMAIN/health/network?strict=true"   # 503 unless everything is reachable
```

`mode` is `direct` on the HK server. On a mainland development machine set `OUTBOUND_PROXY_URL=http://127.0.0.1:7890` (or `socks5h://...`); the same endpoint then reports `proxy`.

## Certificates

certbot renews automatically and nginx reloads every 6 hours. Force a renewal check with `docker compose -p omniflow -f docker-compose.prod.yml run --rm --entrypoint certbot certbot renew`.

## Scaling

- Web: `docker compose -p omniflow -f docker-compose.prod.yml up -d --scale app=3 --no-recreate app`. nginx re-resolves replicas through Docker DNS.
- Keep exactly one `worker`. Extra workers are safe (publishing is claimed atomically) but unnecessary.
- Posts overdue by more than `SCHEDULER_MAX_LATE_SECONDS` (default 1 hour) are marked failed instead of published late.

## Backups

Supabase provides daily backups; enable point-in-time recovery for production. Redis holds no business data. With the SQLite fallback, back up the `app-data` volume.

## Multi-tenancy notes

- Each registered company gets its own tenant. Row Level Security policies in `scripts/init_supabase.sql` are active for any non-owner database role. Create a dedicated role (`CREATE ROLE omniflow_app LOGIN NOBYPASSRLS`) for the app in production.
- The dashboard must send `Authorization: Bearer <token>` on every API call for tenant isolation to apply; anonymous calls use the shared `default` workspace. Set `REQUIRE_AUTH=true` once the dashboard sends tokens.
- Process-wide settings endpoints (`/settings/apis`, default-workspace Facebook token) are restricted to the default workspace.

## File storage (business licenses, ad creatives)

Uploads go to Supabase Storage. Without the two settings below, the API answers uploads with 503 and the 快速开户 form shows a note instead of the upload field.

1. In `.env`, set `SUPABASE_URL` (`https://<project>.supabase.co`) and `SUPABASE_SERVICE_ROLE_KEY` (Supabase → Project Settings → API keys → `service_role` / secret key). This key bypasses Row Level Security: keep it on the server and never send it to the browser.
2. Create the buckets and policies once, and again whenever `scripts/supabase_storage_setup.sql` changes:

   ```bash
   docker compose -p omniflow -f docker-compose.prod.yml run --rm --no-deps -e SCHEDULER_MODE=off app \
     python scripts/migrate.py --sql scripts/supabase_storage_setup.sql
   ```

   It creates `agency-documents` (private) and `ad-creatives` (public), each limited to 10 MB and to PDF/PNG/JPG or PNG/JPG/WebP respectively.
3. Restart the app (`./scripts/deploy_hk.sh --skip-pull`). `curl -s https://$DOMAIN/config/public` should then report `"document_upload_enabled": true`.

Files are stored as `<workspace_id>/<uuid>.<ext>`. The app builds every path from the signed-in user's workspace, so one merchant cannot write into or attach another merchant's files. Business licenses are private: operators open them from the Supabase dashboard (Storage → `agency-documents` → the workspace folder named in `agency_applications.business_license_path`).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| nginx restarts on first boot | Certificate bootstrap failed; rerun the script and read `docker compose logs nginx`. |
| `app` never healthy | `docker compose -p omniflow -f docker-compose.prod.yml logs app`; usually a bad `DATABASE_URL` or a missing `APP_SECRET_KEY`. |
| Posts stay `scheduled` | `logs worker`; confirm `SCHEDULER_MODE=worker` and that the worker container is running. |
| `/health/network` shows `down` | Security group blocks outbound, or the VM is mainland-hosted and needs `OUTBOUND_PROXY_URL`. |
| Facebook posts show Token Expired | Merchant must reconnect the page via `/auth/facebook/login`. |
| License upload says "上传失败" | `logs app` for `Upload to agency-documents failed`: HTTP 404 means the buckets were not created (run the storage SQL above); 403 means a wrong `SUPABASE_SERVICE_ROLE_KEY`. |
