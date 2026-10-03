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

## Plans and billing

There is no free tier. Plans are defined in `app/plans.py`:

| Plan | Price | Limits |
| --- | --- | --- |
| Pro Growth | ¥66/month or ¥666/year | 3 connected channels/Pages, 300 AI runs per 30-day cycle, 5 GB storage |
| Agency VIP | ¥166/month or ¥1666/year | unlimited channels and AI runs, 50 GB storage, priority routing |

- New workspaces start a Pro trial (`SUBSCRIPTION_TRIAL_DAYS`, default 7). Workspaces that existed before billing get the same trial when the migration runs. The `default` workspace is the house account (Agency VIP, no end date); keep `REQUIRE_AUTH=true` in production so nobody else can act as it.
- After the trial or a paid period ends, AI generation and channel binding answer 402 and the dashboard opens the pricing page. Reading data, disconnecting channels and the 快速开户 form keep working.
- AI runs are counted only when the model actually wrote copy; template drafts (AI unavailable) and failed calls are free. Storage sizes and priority routing are listed on the pricing page but not metered yet.
- Payments buy prepaid periods: 30 days (monthly) or 365 days (annual). Nothing renews automatically. Paying for the plan that is running (paid or trial) extends it from its end date; switching plans converts the unused paid days at the two plans' daily prices (15 Pro days become about 6 VIP days). The dashboard warns 7 days before the end and the pricing buttons then read "Renew".

### Online payment: Stripe Checkout

The pricing buttons open Stripe Checkout (card, Alipay, WeChat Pay). Stripe cannot charge WeChat Pay or Alipay again without the customer, which is why each payment is one prepaid period instead of a Stripe subscription. The plan changes only when Stripe confirms the payment: the webhook applies it, and so does the dashboard when the merchant comes back from Stripe (whichever is first; each Checkout Session is applied once, recorded in `billing_payments`).

1. Use a Stripe account whose country supports CNY with Alipay and WeChat Pay (Hong Kong does). In Dashboard → Settings → Payment methods, enable Cards, Alipay and WeChat Pay. `STRIPE_PAYMENT_METHODS` (default `card,alipay,wechat_pay`) only narrows what is enabled there.
2. Developers → API keys: put the secret key in `STRIPE_SECRET_KEY` (a restricted `rk_` key needs write access to Checkout Sessions).
3. Developers → Webhooks → Add endpoint `https://$DOMAIN/api/billing/webhook` with the events `checkout.session.completed`, `checkout.session.async_payment_succeeded` and `checkout.session.async_payment_failed`. Put its signing secret in `STRIPE_WEBHOOK_SECRET`.
4. Set `PUBLIC_BASE_URL=https://$DOMAIN` (Stripe sends merchants back there) and restart (`./scripts/deploy_hk.sh --skip-pull`). `curl -s https://$DOMAIN/api/billing/plans` should report `"payments": {"provider": "stripe", ...}`.
5. Try it in test mode first (`sk_test_` key, the test endpoint's `whsec_` secret): pay with card 4242 4242 4242 4242, or authorise on the Alipay / WeChat Pay test page. On a development machine, `stripe listen --forward-to localhost:8000/api/billing/webhook` forwards events and prints the `whsec_` secret to use. `stripe trigger` events carry no OmniFlow workspace and are acknowledged but ignored.

A paid session whose amount or currency does not match the price list (for example a price changed while a Checkout page was open), or one paid for the house account, is stored with status `review` and not applied; the app log says `kept for review`. Activate it with `set_plan.py` or refund it in Stripe. Refunds are not synced: after refunding in the Stripe Dashboard, run `set_plan.py --expire` if access should end.

### Manual payment (without Stripe)

With `STRIPE_SECRET_KEY` empty, "Upgrade" sends a request instead:

1. The merchant clicks "Upgrade to Pro" or "Contact VIP / Upgrade". The request is stored in `upgrade_requests`, POSTed to `LEAD_NOTIFICATION_WEBHOOK` (event `upgrade_request.created`, with the merchant's email), and the merchant sees the payment link for that plan and cycle if you set `BILLING_CHECKOUT_URL_<PLAN>_<CYCLE>`.
2. Collect payment (WeChat Pay, Alipay, bank transfer, invoice).
3. Activate it (the same period rules as online payments):

   ```bash
   docker compose -p omniflow -f docker-compose.prod.yml run --rm --no-deps -e SCHEDULER_MODE=off app      python scripts/set_plan.py --email boss@shop.com --plan agency --cycle annual
   ```

   `--list` shows pending requests, `--show` a workspace's plan and usage, `--expire` ends access immediately.

The tables (`subscriptions`, `usage_tracking`, `upgrade_requests`, `billing_payments`) and `increment_ai_runs()` come from `scripts/supabase_subscriptions.sql`, which `scripts/migrate.py` applies after `init_supabase.sql` on every deploy. Only the API can change a plan or usage: Supabase clients can at most read their own workspace's rows.

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
| Paid in Stripe but the plan did not change | Stripe Dashboard → Webhooks → the endpoint's recent deliveries: 400 means `STRIPE_WEBHOOK_SECRET` belongs to another endpoint or mode (test vs live), 503 means it is not set. `logs app` for `kept for review`. |
| "暂时无法打开支付页面" on the pricing page | `logs app` for `Stripe Checkout ... failed`: `AuthenticationError` = wrong `STRIPE_SECRET_KEY`; `InvalidRequestError` = a payment method in `STRIPE_PAYMENT_METHODS` is not enabled in the Dashboard or not available in your account's country. |
| License upload says "上传失败" | `logs app` for `Upload to agency-documents failed`: HTTP 404 means the buckets were not created (run the storage SQL above); 403 means a wrong `SUPABASE_SERVICE_ROLE_KEY`. |
