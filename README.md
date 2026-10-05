# FlexyVotes

FlexyVotes is a voting platform with two products on one engine:

1. **Paid public voting** for reality shows, pageants and awards. Fans buy votes through
   Paystack (card, mobile money, bank, USSD). A vote is counted only after Paystack confirms
   the payment server to server.
2. **Institutional secret-ballot elections** for universities, associations, companies,
   unions and churches. Voters are checked against a voter roll. Ballots are encrypted and
   stored with no link to the voter. Results are tallied, certified, signed and
   independently verifiable.

The election engine has no dependency on payments. Payments, fraud scoring, billing and
notifications are separate apps that plug into it.

Event ticketing with QR check-in and a small merchandise store are carried over from the
original FlexyVotes app.

## Contents

- [Feature overview](#feature-overview)
- [Tech stack](#tech-stack)
- [Quick start (Docker)](#quick-start-docker)
- [Quick start (without Docker)](#quick-start-without-docker)
- [Configuration](#configuration)
- [Running tests](#running-tests)
- [Project layout](#project-layout)
- [Documentation](#documentation)

## Feature overview

| Area | What you get |
|---|---|
| Election engine | 11-state lifecycle (Draft → Review → Approved → Scheduled → Open ⇄ Paused → Closed → Tallying → Certified → Published → Archived) with separation of duties, auto open/close, configuration freezes and edit locks. |
| Ballot types | Single choice, FPTP, multiple choice (min/max), approval, ranked (IRV for 1 seat, STV for several), score and referendum (with threshold). Abstention per position. |
| Voters | CSV/XLSX import, constituency tree, eligibility rules, self-registration by email domain, invitations, credential reset, turnout. |
| Voter sign-in | Voter ID + access code, email OTP, SMS OTP, institutional SSO (Google / Microsoft OIDC), LDAP / Active Directory, platform account; optional second factor. |
| Ballot secrecy | Identity check → single-use authorization → anonymous ballot. Ballots are sealed with per-election X25519 keys (optionally split between trustees, k-of-n). Ballot rows have no voter, no token and no timestamp. |
| Verifiability | Ballot tracker per voter, Merkle-tree bulletin board with inclusion proofs, Ed25519-signed config snapshots and results, a downloadable verification bundle and an offline verifier (`tools/verify_election.py`). |
| Paid voting | Paystack init/verify/webhooks (HMAC-SHA512), vote bundles, discount codes, spending limits, refunds (with dual approval above a threshold), chargebacks, reconciliation, revenue reports. |
| Fraud | Weighted risk score (0–100) per payment: ALLOW / MONITOR / CHALLENGE / HOLD. Held payments wait for an analyst. Background anomaly scans. Blocklist. |
| Integrity | Dual approval for sensitive actions, disputes, incidents, evidence with SHA-256, legal hold, recounts. |
| Access control | 43 permissions, 12 roles, scoped to the platform, an organization or a single election. |
| Audit | Hash-chained, append-only audit log per organization (enforced by PostgreSQL triggers), verified every 6 hours. |
| Accounts | Argon2 passwords, lockout, TOTP 2FA with recovery codes, passkeys (WebAuthn), session list and revocation, new-device alerts, API tokens. |
| Results | Live or after-close or after-publish visibility; CSV, Excel and PDF exports; JSON API. |
| Platform | Multi-tenant organizations, SaaS billing (plans, limits, invoices with Ghana levy and VAT, coupons), notifications (email, SMS, WhatsApp, in-app), candidate portal, support desk. |
| Operations | Celery + Redis jobs, Prometheus metrics, JSON logs with correlation IDs, Sentry, OpenTelemetry, health checks, encrypted backups with tested restores. |
| UX | Works without JavaScript, WCAG 2.1 AA target, high-contrast / large-text / low-bandwidth modes, English and French. |

A section-by-section coverage map against the requirements is in
[docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md).

## Tech stack

- **Python 3.12**, **Django 6.0**, **django-ninja** (REST API with OpenAPI)
- **PostgreSQL 16** (production, CI); SQLite for quick local runs
- **Redis 7**: cache, rate limits, Celery broker
- **Celery 5** worker and beat
- **cryptography** (AES-GCM, X25519, Ed25519, HKDF, scrypt), optional **AWS KMS**
- Paystack, Africa's Talking (SMS/USSD), WhatsApp Cloud API, SMTP
- Gunicorn, WhiteNoise, nginx (production), Cloudinary for public media
- Bootstrap 5 with a strict nonce-based Content Security Policy

## Quick start (Docker)

The full stack runs with Docker Compose: PostgreSQL, Redis, web (Gunicorn), Celery worker
and Celery beat. pgAdmin is optional.

```bash
cp .env.example .env
# For local use, set at least:
#   DEBUG=True  SECRET_KEY=<any long random string>
#   ALLOWED_HOSTS=localhost,127.0.0.1  SITE_URL=http://localhost:8000
#   SECURE_SSL_REDIRECT=False  SECURE_HSTS_SECONDS=0  DATABASE_SSL_REQUIRE=False
#   DJANGO_SUPERUSER_USERNAME / _EMAIL / _PASSWORD for the first admin
docker compose up --build
```

- App: `http://localhost:8000/`, or another port if you set `PORT` in `.env`.
- Staff console: `/console/`. Django admin: `/admin/`, or whatever `ADMIN_URL` is set to.
- API docs: `/api/v1/docs`.
- pgAdmin: `docker compose --profile admin up -d pgadmin`, then `http://localhost:5050/`.

Migrations run automatically when the web container starts. The admin from
`DJANGO_SUPERUSER_*` is created, or updated to match, on every start.

With `DEBUG=True` and no `PAYSTACK_SECRET_KEY`, paid voting uses a built-in **payment
simulator**, so the whole pay-to-vote flow can be tried without a Paystack account. The
simulator is refused when `DEBUG=False`.

## Quick start (without Docker)

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # Windows
# source .venv/bin/activate         # macOS / Linux
pip install -r requirements-dev.txt
cp .env.example .env                # set DEBUG=True and SECRET_KEY at minimum
python manage.py migrate
python manage.py compilemessages    # French translations (needs GNU gettext)
python manage.py createsuperuser
python manage.py runserver
```

Without `DATABASE_URL` the app uses SQLite (`db.sqlite3`). Without `REDIS_URL` it uses an
in-process cache, and Celery tasks run inline (eager mode), so no worker is needed.

## Configuration

All configuration comes from environment variables, which are loaded from `.env`.
[.env.example](.env.example) lists every variable, grouped and commented. The full
reference, with defaults and what each one does, is in
[docs/OPERATIONS.md](docs/OPERATIONS.md#environment-variables).

Production needs dedicated keys, not keys derived from `SECRET_KEY`:

```bash
python manage.py keys generate-kek            # FIELD_ENCRYPTION_KEYS (or use KMS_KEY_ID)
python manage.py keys generate-signing-key    # SIGNING_PRIVATE_KEY
python manage.py keys generate-blind-index    # BLIND_INDEX_KEY
python manage.py check --deploy               # warns about anything still missing
```

## Running tests

```bash
python manage.py test                                         # SQLite, fast
docker compose run --rm web python manage.py test --noinput   # PostgreSQL
```

The suite has 204 tests: unit, integration, end-to-end web flows, security, and
race-condition tests.
- **SQLite:** the 5 concurrency tests and the trigger test need PostgreSQL and are skipped.
- **PostgreSQL:** all tests run except 1 SQLite-only tamper test. It edits a ballot row
  directly, which the append-only trigger blocks on PostgreSQL.

CI (`.github/workflows/ci.yml`) runs:
- the suite on PostgreSQL + Redis;
- Bandit and pip-audit;
- a Trivy image scan;
- an OWASP ZAP baseline scan.

Details: [docs/TESTING.md](docs/TESTING.md).

## Project layout

| Path | Purpose |
|---|---|
| `vote_fund/` | Django project: settings, URLs, Celery app, WSGI/ASGI |
| `core/` | Platform services: crypto, RBAC, audit, auth/MFA/SSO/LDAP, rate limits, idempotency, middleware, tenancy, staff console |
| `voting/` | Events, positions (`Category`), candidates, paid-vote ledger, ticketing, store, public pages, USSD |
| `elections/` | Institutional elections: voters, constituencies, eligibility, ballots, keys, tally, results, verification, integrity, candidate portal |
| `payments/` | Paystack client, payment state machine, webhooks, refunds, reconciliation, pricing |
| `fraud/` | Risk engine, alerts, blocklist, anomaly scans |
| `notifications/` | Outbox and delivery over email, SMS, WhatsApp and in-app |
| `billing/` | Plans, subscriptions, usage, invoices, coupons, feature flags |
| `api/` | REST API v1 (`/api/v1/`) |
| `templates/`, `locale/` | Server-rendered UI and translations |
| `deploy/` | nginx, Prometheus rules, backup / restore scripts |
| `tools/verify_election.py` | Offline verifier for published elections |

The full annotated tree is in [docs/PROJECT_TREE.md](docs/PROJECT_TREE.md).

## Documentation

| Document | Contents |
|---|---|
| [PRD](docs/PRD.md) | Product goals, users, roles and requirements |
| [TRD](docs/TRD.md) | Technical design: engine, secrecy model, crypto, payments, jobs, constraints |
| [Architecture](docs/ARCHITECTURE.md) | Components, request and data flows, trust boundaries |
| [API](docs/API.md) | REST API v1, webhooks, USSD, page routes |
| [Database](docs/DATABASE.md) | Schema by app, constraints, append-only tables, encryption at rest |
| [Security](docs/SECURITY.md) | Security controls, threat model and the vulnerability remediation log |
| [Testing](docs/TESTING.md) | How to run tests, what they cover, CI and security scans |
| [Deployment](docs/DEPLOYMENT.md) | Docker on AWS (ECS Fargate or EC2), step by step |
| [Operations](docs/OPERATIONS.md) | Environment variables, runbooks, monitoring, key management |
| [Disaster recovery](docs/DISASTER_RECOVERY.md) | RPO / RTO, backups, PITR, restore testing, failover |
| [Project tree](docs/PROJECT_TREE.md) | Annotated directory layout |
| [Implementation status](docs/IMPLEMENTATION_STATUS.md) | Requirements coverage, mapped to code and tests |
| [Features spec](docs/FEATURES.md) | The original feature requirements |
