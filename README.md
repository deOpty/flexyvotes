# FlexyVotes

FlexyVotes is a Django-based event voting, ticketing, and fundraising platform built for the Ghanaian market. It lets approved organizers run paid online voting competitions (e.g. pageants, awards, talent shows), sell event tickets with QR-code check-in, and reach voters/buyers who only have a basic phone via USSD (Africa's Talking).

Payments are processed through Paystack (card and mobile money) for web users, and through Africa's Talking mobile money for USSD users. All prices are in Ghanaian Cedis (GHS), and 1 GHS = 1 vote under the "Pay to Vote" mode.

## Table of Contents

- [Features](#features)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Local Setup](#local-setup)
- [Environment Variables](#environment-variables)
- [How Voting Works](#how-voting-works)
- [How Ticketing Works](#how-ticketing-works)
- [USSD Flow](#ussd-flow)
- [Running Tests](#running-tests)
- [Project Structure](#project-structure)
- [Related Documentation](#related-documentation)

## Features

- **Organizer accounts** — anyone can register; an admin must approve an account (`Profile.is_approved_organizer`) via Django admin before it can create events.
- **Events** with a title, description, start/end dates, theme colors, background/flyer images, an active flag, and a configurable platform fee percentage used to compute organizer payouts.
- **Categories & Candidates** — candidates are grouped into categories within an event (e.g. "Best Male", "Best Female"). Each candidate gets an auto-generated unique nominee code (e.g. `TE025`) used for USSD and code voting.
- **Two voting modes, set per event:**
  - **Pay to Vote** — voters pay via Paystack (card or mobile money); 1 GHS = 1 vote.
  - **Code Voting** — organizers generate one-time voting codes (optionally paired with a Student ID) that voters redeem instead of paying online.
- **Tie-breaker voting** — an optional per-event toggle that lets a _paid web ticket_ (reference starting with `TK-`) be redeemed once for a bonus "Tie-Breaker" vote, separate from the main vote tally.
- **USSD access (Africa's Talking)** — phone users without smartphones can vote (enter nominee code + number of votes, pay via mobile money) or buy a ticket entirely through a USSD menu, with no web app required.
- **Live results** — event detail page shows vote counts and percentages (main votes and tie-breaker votes tracked separately), plus a live-count polling endpoint.
- **Ticketing** — organizers create tickets per event (name, price, discount/old price, quantity available, image). Buyers purchase via Paystack (web) or USSD; each purchase gets a unique reference and a QR-code e-ticket emailed to the buyer.
- **QR check-in / scanner** — organizers/staff can scan a ticket's QR code at the door (`event_scanner` / `process_scan`) to validate and mark it checked-in; there's also a manual ticket verification/retrieval flow for buyers who lose their email.
- **Guest list** — organizers can view and export (CSV) the list of ticket buyers/check-ins for an event.
- **CSV tools** — bulk-add candidates and bulk-upload a student ID list (for Student ID + Code voting) via CSV; CSV output is sanitized against macro injection.
- **Merchandise store** — a simple Product/ProductCategory catalog, managed by staff, shown on a public store page.
- **Activity log** — an `ActivityLog` model records notable organizer/admin actions against an event.
- **Analytics dashboard** — per-event revenue, vote breakdowns, and organizer payout (after platform fee) shown to organizers/staff.
- **Cloudinary media storage** — all uploaded images (candidates, events, products, tickets) are stored on Cloudinary in every environment.

## Tech Stack

- **Backend:** Django 6.0.7 (Python 3.12)
- **Database:** PostgreSQL in production (via `dj-database-url` / `DATABASE_URL`), SQLite for local development
- **Media storage:** Cloudinary (`cloudinary`, `django-cloudinary-storage`)
- **Static files:** Whitenoise (`CompressedManifestStaticFilesStorage`)
- **Payments:** Paystack (web/card/mobile money), Africa's Talking (`africastalking` SDK) for USSD mobile money
- **QR codes:** `qrcode` + `Pillow` for generating e-ticket QR images
- **App server:** Gunicorn (see `Procfile`)
- **Config:** `python-dotenv` (loads `.env`), environment-variable-driven `settings.py`

## Prerequisites

- Python 3.12 (see `runtime.txt`)
- pip
- A Paystack account (test keys are fine for local dev)
- A Cloudinary account (free tier is fine)
- An Africa's Talking account (the `sandbox` username works for local testing)
- A Gmail account with an [App Password](https://support.google.com/accounts/answer/185833) for sending ticket/approval emails (optional for basic local dev)
- PostgreSQL, if you want to run against Postgres locally instead of the SQLite default

## Local Setup

```bash
# 1. Clone the repository and enter it
git clone <repo-url>
cd flexyvotes

# 2. Create and activate a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\Activate.ps1
# macOS/Linux:
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# then edit .env with your own values (see below)

# 5. Apply database migrations
python manage.py migrate

# 6. Create an admin/superuser account
python manage.py createsuperuser

# 7. Run the development server
python manage.py runserver
```

The app will be available at `http://127.0.0.1:8000/`. Log in to `/admin/` with your superuser to approve organizer accounts, manage events/candidates/tickets, and moderate the store catalog.

Without a `DATABASE_URL` set, the app falls back to a local SQLite database (`db.sqlite3` in the project root); `db.sqlite3`, `.env`, and `media/` are git-ignored.

## Environment Variables

All configuration is read from environment variables (loaded from `.env` via `python-dotenv`). See `.env.example` for a template.

| Variable                | Required                           | Description                                                                                                            |
| ----------------------- | ---------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `SECRET_KEY`            | Yes                                | Django cryptographic secret key. Must be long and random in production.                                                |
| `DEBUG`                 | No (default `False`)               | Set to `True` for local development to get Django's debug pages.                                                       |
| `ALLOWED_HOSTS`         | No                                 | Comma-separated list of allowed hostnames. Defaults include `localhost,127.0.0.1,0.0.0.0,flexyvotes.onrender.com`.     |
| `CSRF_TRUSTED_ORIGINS`  | Recommended in production          | Comma-separated list of trusted origins (must include scheme, e.g. `https://your-domain.example.com`) for CSRF checks. |
| `SITE_URL`              | Yes for payments                   | Public base URL used to build Paystack callback/webhook URLs. Defaults to `http://127.0.0.1:8000` locally.             |
| `DATABASE_URL`          | No (SQLite used if unset)          | Postgres connection string, e.g. `postgres://user:password@host:5432/dbname`. When set, `ssl_require=True` is applied. |
| `PAYSTACK_SECRET_KEY`   | Yes for web payments               | Paystack secret key (test or live) used to initialize payments and verify the webhook signature.                       |
| `AT_USERNAME`           | Yes for USSD/mobile money          | Africa's Talking username (`sandbox` for testing).                                                                     |
| `AT_API_KEY`            | Yes for USSD/mobile money          | Africa's Talking API key.                                                                                              |
| `EMAIL_HOST_USER`       | Yes for emailing tickets/approvals | Gmail address used as the SMTP login and default "from" address.                                                       |
| `EMAIL_HOST_PASSWORD`   | Yes for emailing tickets/approvals | Gmail App Password (SMTP over TLS on port 587).                                                                        |
| `CLOUDINARY_CLOUD_NAME` | Yes                                | Cloudinary cloud name.                                                                                                 |
| `CLOUDINARY_API_KEY`    | Yes                                | Cloudinary API key.                                                                                                    |
| `CLOUDINARY_API_SECRET` | Yes                                | Cloudinary API secret.                                                                                                 |
| `SECURE_SSL_REDIRECT`   | No (default `False`)               | Only consulted when `DEBUG=False`. Set to `True` once HTTPS is actually terminated in front of the app.                |
| `SECURE_HSTS_SECONDS`   | No (default `0`)                   | Only consulted when `DEBUG=False`. HSTS is only enabled once this is > 0.                                              |
| `DJANGO_LOG_LEVEL`      | No (default `INFO`)                | Root logger level.                                                                                                     |

Notes:

- Media files (`MEDIA_URL`/`MEDIA_ROOT`) are configured for local/DEBUG serving, but all uploaded files (candidate, event, product, and ticket images) are actually stored via Cloudinary (`STORAGES['default'] = cloudinary_storage.storage.MediaCloudinaryStorage`) in every environment — Cloudinary credentials are required for image uploads to work at all. This Django version only honors the `STORAGES` setting for storage resolution, not the legacy `DEFAULT_FILE_STORAGE` alone, so both are kept defined and in sync in `settings.py`.
- Session/CSRF cookies are marked `Secure` automatically whenever `DEBUG=False`.

## How Voting Works

Each `Event` has a `voting_mode` of either **Pay to Vote** or **Code Voting**, and votes are recorded as `VoteTransaction` rows tied to a `Candidate`.

**Pay to Vote**

1. A voter picks a candidate on the event page and enters a number of votes.
2. `initiate_vote` creates a `Pending` `VoteTransaction` and redirects the voter to a Paystack checkout (1 GHS per vote).
3. On success, Paystack redirects back to `vote_success` and/or calls the `paystack_webhook` endpoint (verified via the `x-paystack-signature` HMAC header using `PAYSTACK_SECRET_KEY`), which marks the transaction `Success`.
4. The event detail page aggregates `Success` transactions per candidate to show live vote counts and percentages.

**Code Voting**

1. Organizers generate a batch of one-time `VotingCode`s for their event (optionally requiring a Student ID via `code_voting_mode = 'Student ID'`, uploaded/matched via CSV).
2. Voters redeem a code (and Student ID, if required) against a candidate through `cast_vote_with_code`; the code is marked used and a `VoteTransaction` is recorded — no payment step involved.

**Tie-breaker votes (optional, per event)**

- If `enable_tie_breaker` is on, a buyer of a **web** event ticket can redeem their ticket reference (prefixed `TK-`) once through the same code-voting screen to cast one extra "Tie-Breaker" vote, tracked separately from main votes so it doesn't distort the primary tally.

## How Ticketing Works

1. Organizers create one or more `Ticket` types per event (name, price, optional discounted "old price", quantity available, image).
2. Buyers purchase via the web (`buy_ticket`, paid through Paystack) or via USSD (paid through Africa's Talking mobile money). Each purchase becomes a `TicketPurchase` with a unique `paystack_reference`, buyer name/email, quantity, and a `purchase_method` of `Web` or `USSD`.
3. On a successful purchase, a QR-code e-ticket (generated with `qrcode`/`Pillow`) is emailed to the buyer (`send_ticket_email`); buyers can also retrieve/re-send a ticket by reference (`retrieve_ticket_view`) if the email is lost.
4. At the door, organizer/staff accounts use the QR scanner page (`event_scanner`) to scan and validate a ticket; `process_scan` looks up the `TicketPurchase`, checks it hasn't already been checked in, and marks `is_checked_in` / `checked_in_at`.
5. Organizers can view and export (CSV) the event guest list of purchases/check-ins.

## USSD Flow

The `ussd_callback` endpoint implements an Africa's Talking USSD session as a simple menu tree (no smartphone or internet needed):

- **Main menu:** `1` Vote for Candidate, `2` Buy Event Ticket.
- **Voting branch:** enter a candidate's nominee code → enter number of votes (1 GHS = 1 vote) → confirm → mobile money checkout is triggered via `trigger_mobile_money_checkout` (Africa's Talking Payments).
- **Ticket branch:** similarly walks the caller through selecting an event/ticket and paying by mobile money, recording the purchase as `purchase_method = 'USSD'`.

## Running Tests

The project ships an automated test suite in `voting/tests.py`. Run it with:

```bash
python manage.py test
```

This uses Django's test runner (an isolated test database is created and destroyed automatically — no changes to your dev database or `.env` are required).

## Project Structure

See [`PROJECT_TREE.md`](./docs/PROJECT_TREE.md) for a full directory layout. At a glance:

- `vote_fund/` — Django project package (`settings.py`, root `urls.py`, `wsgi.py`)
- `voting/` — the single Django app containing all models, views, URLs, admin config, services (Paystack), `at_service.py` (Africa's Talking), and tests
- `templates/voting/` — server-rendered HTML templates
- `media/` — local media (git-ignored; production media lives on Cloudinary)
- `requirements.txt`, `runtime.txt`, `Procfile` — dependency, Python version, and process definitions for deployment

## Related Documentation

This README covers setup and a functional overview. For deeper detail, see the other documents in this `docs/` folder:

- [`PRD.md`](./docs/PRD.md) — Product Requirements Document: goals, user roles, and feature scope.
- [`TRD.md`](./docs/TRD.md) — Technical Requirements Document: implementation-level requirements and constraints.
- [`PROJECT_TREE.md`](./docs/PROJECT_TREE.md) — Full annotated directory/file listing.
- [`ARCHITECTURE.md`](./docs/ARCHITECTURE.md) — System architecture, request flow, and integration diagrams (Paystack, Africa's Talking, Cloudinary).
- [`API.md`](./docs/API.md) — Endpoint/URL reference, including webhook and USSD callback contracts.
- [`DATABASE.md`](./docs/DATABASE.md) — Data model and schema reference.
- [`DEPLOYMENT.md`](./docs/DEPLOYMENT.md) — Deployment steps (Gunicorn, Docker, AWS).
- [`TESTING.md`](./docs/TESTING.md) — Test suite structure and coverage notes.
- [`SECURITY.md`](./docs/SECURITY.md) — Security posture, known hardening decisions, and past fixes.
- [`OPERATIONS.md`](./docs/OPERATIONS.md) — Runbooks, monitoring, and operational procedures.
