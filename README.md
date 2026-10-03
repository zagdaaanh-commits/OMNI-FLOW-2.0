# OmniFlow AI 2.0: Multi-Agent Social Media Marketing System

Production-grade autonomous multi-agent marketing platform built with FastAPI, Pydantic v2, APScheduler, and SQLite. Features an Apple Cupertino / visionOS dark & light mode dashboard, real user authentication, multi-platform copy generation, and official social API connectivity.

---

## Supported Ecosystems

- **Global Channels**: Meta (Facebook Graph API), Instagram Professional, TikTok Commercial API, X (Twitter API v2).
- **APAC & Cross-Border**: Xiaohongshu (RED) Open Platform, Douyin, WeChat Official Accounts.

The architecture decouples platform-specific API and automation adapters, allowing seamless scaling when permissions, API versions, or endpoints change.

---

## Autonomous Swarm Architecture

```text
OmniFlow 2.0 Client / VisionOS Studio
       |
     FastAPI Backend (Port 5000)
       |
       +--> CampaignPlanner
       |       |- Global strategy & target audience
       |       |- Real-time dynamic budget allocation
       |       \- Automated multi-platform posting schedule
       |
       +--> CopywriterAgent
       |       |- Multi-lingual copy generation (EN, ZH)
       |       |- Tone mapping per feed environment
       |       \- Hashtags, CTAs, and viral score prediction
       |
       +--> PublisherAgent
       |       +--> MetaAPIClient (Facebook & Instagram)
       |       +--> TikTok & X Official API Adapters
       |       \- RPA & Production Gateway Dispatcher
       |
       +--> AnalyticsAgent
       |       |- Cross-channel blended metrics (Impressions, Clicks, Spend)
       |       |- Target ROAS calculations & margin audits
       |       \- AI budget reallocation recommendations
       |
       \--> SQLiteStore (Auth & Social Connection Layer)
               |- Users & Hashed Password Authentication
               |- Connected Social Accounts & Encrypted Tokens
               \- Campaigns, Drafts, and Scheduling Tasks
```

---

## Directory Structure

```text
c:/ZGC HACKATHON/
├── app/
│   ├── main.py              # Core FastAPI application, Auth & Integrations APIs
│   ├── dashboard.py         # Fallback analytics dashboard
│   └── static/
│       ├── index.html       # Apple visionOS & Cupertino Light/Dark Web Suite
│       └── assets/
│           ├── logos/       # Vector SVGs (Meta, TikTok, RED, WeChat, IG, X)
│           └── images/      # High-resolution marketing creatives
├── agents/
│   ├── analytics.py         # Multi-platform ROI & audience analytics agent
│   ├── copywriter.py        # Feed-optimized copywriter agent
│   ├── planner.py           # Campaign strategist & budget allocator
│   └── publisher.py         # Publishing & scheduling dispatcher
├── models/
│   └── schemas.py           # Pydantic v2 domain models, Auth & OAuth schemas
├── storage.py               # SQLite persistence, user auth & social accounts
├── tests/
│   ├── test_api_endpoints.py# 10 integration tests for all REST endpoints
│   └── test_system.py       # End-to-end multi-agent pipeline tests
```

---

## Getting Started

### 1. Requirements
- Python 3.11+ (Python 3.14 compatible)
- SQLite3

### 2. Installation
```powershell
python -m pip install -r requirements.txt
```

### 3. Launch Development Server
```powershell
uvicorn app.main:app --host 127.0.0.1 --port 5000 --reload
```

Open your browser at:
`http://127.0.0.1:5000/`

---

## Authentication & API Endpoints

### User Authentication
- `POST /auth/register`: Create a new user account with secure password hashing.
- `POST /auth/login`: Authenticate email and password, returns session token.
- `GET /auth/me`: Retrieve current logged-in profile.
- `GET /auth/users`: List all registered system users.

### Social Channel Integrations
- `GET /integrations/status`: Query connection status for Meta, TikTok, X, WeChat, RED.
- `POST /integrations/connect`: Connect and verify official platform credentials.
- `POST /integrations/disconnect`: Disconnect an active account.
- `GET /auth/oauth/meta/url`: Generate official Meta OAuth2 authorization URL.

### Plans & Billing (Pro Growth / Agency VIP, no free tier)
- `GET /api/billing/plans`: Public price list (¥66/¥666 Pro, ¥166/¥1666 Agency VIP).
- `GET /api/billing/subscription`: The workspace's plan, status (trial / active / expired), limits and usage.
- `POST /api/billing/create-checkout-session`: Stripe Checkout (card, Alipay, WeChat Pay) for one prepaid period: `{"plan": "pro"|"agency", "cycle": "monthly"|"annual"}` → `{"url": ...}`. The workspace travels in `client_reference_id` and `metadata.workspace_id`.
- `POST /api/billing/webhook`: Stripe webhook (signature checked with `STRIPE_WEBHOOK_SECRET`); `checkout.session.completed` activates the plan for 30 / 365 days, extending a running period.
- `GET /api/billing/checkout-session/{id}`: Used by the dashboard when the merchant returns from Stripe; applies a paid session immediately (once, shared with the webhook).
- `POST /api/billing/upgrade-request`: Manual path when Stripe is not configured; records a request, and the operator activates the plan after payment with `scripts/set_plan.py`.
- AI generation (`/content/generate`, `/assistant/chat`) and channel binding are gated by `app/dependencies/limits.py`: 402 `SUBSCRIPTION_REQUIRED`, 403 `AI_LIMIT_REACHED` (Pro: 300 runs per 30 days) or 403 `CHANNEL_LIMIT_REACHED` (Pro: 3 channels).

### Agency Applications & File Storage (Supabase Storage)
- `POST /api/upload/document`: Business license (PDF / PNG / JPG, ≤ 10 MB) into the private `agency-documents` bucket; returns its storage path and a 10-minute signed URL.
- `POST /api/upload/creative`: Campaign image (PNG / JPG / WebP, ≤ 10 MB) into the public `ad-creatives` bucket; returns its public URL.
- `POST /api/agency/apply`: Meta agency ad-account application; `business_license_path` attaches a document uploaded by the same workspace.
- Setup: `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`, then `python scripts/migrate.py --sql scripts/supabase_storage_setup.sql` (see `docs/DEPLOYMENT.md`).

### Autonomous Marketing Suite
- `POST /campaign/create`: Create campaign with automated budget allocation.
- `GET /campaigns`: List active and planned campaigns.
- `POST /content/generate`: Multi-agent copy synthesis across target channels.
- `POST /publish/schedule`: Immediate dispatch or automated queue scheduling.
- `GET /analytics/report`: Cross-platform impressions, clicks, spend, and ROAS audit.
- `POST /campaign/boost`: Overclock swarm performance with 10X acceleration telemetry.

---

## Testing & Verification

Run the full automated test suite:
```powershell
python -m pytest
```
