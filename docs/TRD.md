# FlexyVotes — Technical Requirements & Design Document

## 1. System Overview

FlexyVotes is a Django 6.0.7 web application that lets event organizers run
paid or code-based voting competitions (e.g. "Best Male", "Best Female"
pageant-style contests), sell event tickets, and run a small merchandise
store. It supports three voter-facing channels:

- **Web** — browse events, vote for a candidate by paying via Paystack, buy
  tickets, redeem voting codes.
- **USSD** — a full menu-driven flow (via Africa's Talking) that lets a
  feature-phone user vote for a candidate or buy a ticket using only a USSD
  session (`*XXX#`), with no internet access required.
- **Admin/Organizer dashboard** — Django views (not the Django admin) for
  approved organizers to create events, candidates, categories, tickets,
  voting codes, and view analytics; the Django admin site itself is used for
  superuser tasks (approving organizers, direct record editing).

The project is a single Django project (`vote_fund`) with a single Django
app (`voting`) that contains all models, views, and integration code.

## 2. Technology Stack

| Layer | Technology | Version (requirements.txt) |
|---|---|---|
| Language / runtime | Python | 3.12.4 (`runtime.txt`) |
| Web framework | Django | 6.0.7 |
| WSGI server | gunicorn | 26.0.0 |
| ASGI/WSGI glue | asgiref | 3.12.1 |
| Database driver (Postgres) | psycopg2-binary | 2.9.12 |
| Database URL parsing | dj-database-url | 3.1.2 |
| Static file serving | whitenoise | 6.12.0 |
| Media storage | cloudinary + django-cloudinary-storage | 1.45.0 / 0.3.0 |
| Payments | Paystack (HTTP API via `requests`) | requests 2.34.2 |
| USSD / mobile money | africastalking | 2.0.2 |
| QR codes | qrcode | 8.2 |
| Image processing | pillow | 12.3.0 |
| Env config | python-dotenv | 1.2.2 |
| Misc | certifi, charset-normalizer, idna, urllib3, packaging, colorama, PyYAML, responses, schema, six, sqlparse, tzdata | pinned, transitive/test deps |

Database: SQLite (`db.sqlite3`) for local development, Postgres in
production via `DATABASE_URL` (see §7). Caching: Django `LocMemCache` (see
§8).

## 3. Application Architecture

FlexyVotes follows Django's standard MVT (Model-View-Template) pattern.

- **Project (`vote_fund/`)**: `settings.py`, `urls.py`, `wsgi.py`, `asgi.py`.
  `wsgi.py` is what gunicorn serves in production per the `Procfile`
  (`gunicorn vote_fund.wsgi:application --log-file -`); `asgi.py` exists
  (Django's default scaffold) but is not referenced by the Procfile or any
  deployment config — the app runs as a synchronous WSGI app end to end.
- **App (`voting/`)**: `models.py` (all data models), `views.py` (all
  request handlers — a single ~1500-line module covering web pages, the
  Paystack webhook, and the USSD callback), `services.py` (Paystack HTTP
  client), `at_service.py` (Africa's Talking mobile-money helper, currently
  unused — see §10), `admin.py` (Django admin customization + organizer
  approval emails), `urls.py` (app-level routes, included from the project
  `urls.py`).
- **Routing**: `vote_fund/urls.py` mounts `voting.urls` at `/`, registers
  `/admin/`, and separately registers `/ussd/callback/` twice (once directly
  in the project urls and once again inside `voting/urls.py` — both point at
  the same `views.ussd_callback`, so the route is effectively duplicated;
  worth cleaning up).
- **Templates**: `django.contrib.staticfiles`-style app templates dir plus a
  project-level `templates/` directory (`TEMPLATES.DIRS`).
- **No REST framework / API layer**: all "API-like" endpoints
  (`live_vote_counts`, `send_ticket_email`, `process_scan`, `ussd_callback`,
  `paystack_webhook`) are plain Django views returning `JsonResponse` or
  `HttpResponse`, not DRF.

Full entity/relationship documentation lives in the companion
[`DATABASE.md`](./DATABASE.md) — this document only summarizes the model
set (see §5).

## 4. Request Lifecycle — Key Flows

### 4.1 Paystack Pay-to-Vote Flow

1. Voter is on `event_detail.html`, submits a POST to
   `initiate_vote(request, candidate_id)` (`voting/urls.py` →
   `vote/<int:candidate_id>/`) with a whole-number `amount` (1 GHS = 1 vote).
2. `initiate_vote` blocks staff/approved-organizer accounts from voting,
   validates `amount >= 1`, and calls
   `services.initialize_paystack_payment(voter_email, amount, candidate_id)`
   with a hardcoded placeholder voter email (`anonymous@FlexyVotes.com` —
   the app does not currently collect a real voter email for web pay-to-vote).
3. `services._paystack_initialize` generates a random UUID4 `reference`,
   POSTs to `https://api.paystack.co/transaction/initialize` with a Bearer
   token (`PAYSTACK_SECRET_KEY`), amount converted to kobo/pesewas
   (`int(amount * 100)`), and a `callback_url` of
   `f"{SITE_URL}/vote/success/"`. On any `requests.RequestException` or a
   falsy `status` in Paystack's JSON response it logs and returns
   `(None, None)` — the caller then just redirects back to `event_detail`
   with no vote recorded (no user-facing error message is shown in that
   path, only a page redirect).
4. On success, `initiate_vote` creates a `VoteTransaction` row with
   `status='Pending'` and the Paystack `reference`, then redirects the
   browser to Paystack's hosted `authorization_url`.
5. The voter completes payment on Paystack's site. Two independent paths can
   mark the transaction `Success`:
   - **Webhook** (`paystack_webhook`, `POST /webhook/paystack/`,
     `@csrf_exempt`): verifies `X-Paystack-Signature` via
     `hmac.new(secret, request.body, hashlib.sha512)` compared with
     `hmac.compare_digest`; rejects with HTTP 400 on mismatch or invalid
     JSON. On `event == 'charge.success'` it looks up the transaction (or
     `TicketPurchase` if `metadata.type == 'ticket_purchase'`) by reference
     and flips `Pending → Success`. Missing records are silently ignored
     (`DoesNotExist` swallowed).
   - **Browser redirect fallback** (`vote_success`, `GET /vote/success/`):
     reads `?reference=`, and if the matching `VoteTransaction` is still
     `Pending`, marks it `Success` directly — with **no signature or
     payment-status verification against Paystack**. This is explicitly
     commented as "Fallback for local testing" but ships in the same code
     path used in production, meaning anyone who guesses/observes a pending
     reference and hits this URL can mark it successful without ever
     paying. This is a real technical-debt/security item (see §10).
6. Vote counts are derived, not stored — `Candidate` has no vote counter
   field; `event_detail`/`live_vote_counts`/`event_analytics` all recompute
   `Sum('transactions__number_of_votes', filter=Q(status='Success', ...))`
   on every request.

### 4.2 USSD Voting State Machine

1. Africa's Talking POSTs to `/ussd/callback/` (`views.ussd_callback`,
   `@csrf_exempt`, since AT is an external caller with no Django session/
   CSRF token) on every keypress in the session, with `sessionId`,
   `serviceCode`, `phoneNumber`, and a cumulative `text` field containing
   every input the user has typed so far, separated by `*`
   (e.g. `"1*TE025*5*1"`).
2. The handler splits `text` on `*` into `inputs` and branches purely on
   `len(inputs)` and `inputs[0]` — there is no server-side session storage;
   the entire state machine is reconstructed from the AT-supplied `text` on
   every request (this is the standard AT USSD pattern, but it means every
   step re-queries the DB for lookups already performed in prior steps,
   e.g. re-fetching the event/ticket list at levels 2–6 of the ticket flow).
3. **Menu (text == "")**: returns `CON` (continue-session) response
   `"1. Vote for Candidate\n2. Buy Event Ticket"`.
4. **Voting branch (`inputs[0] == "1"`)**: level 1 asks for a nominee code;
   level 2 looks up `Candidate.objects.filter(nominee_code=code_input)` and
   asks for a vote count; level 3 computes `total_cost = votes * 1` (hardcoded
   1 GHS/vote) and asks for confirmation; level 4, on confirm, immediately
   creates a `VoteTransaction` with `status='Success'` and a synthetic
   reference `USSD_<hex8>` — **no Paystack or Africa's Talking mobile-money
   charge is actually triggered**; the vote is recorded as paid without any
   real payment collection. (`at_service.trigger_mobile_money_checkout`
   exists for this purpose but is never called from `ussd_callback` — see
   §10.)
5. **Ticket branch (`inputs[0] == "2"`)**: levels 1–5 walk the user through
   selecting an event (by list index, re-queried and re-indexed at every
   step), a ticket type, a quantity, and a name, ending in a confirmation
   prompt. Level 6, on confirm, creates a `TicketPurchase` with
   `status='Success'` and `purchase_method='USSD'`, again with **no real
   payment step** — the reference is synthesized client-side
   (`TK-<2 letters><4 digits>`) the same way `services.initialize_ticket_payment`
   does for the web flow, but here it's assigned success status unconditionally.
6. All terminal responses use `END` (closes the USSD session); all
   intermediate ones use `CON` (keeps the session open for another digit).
7. Because USSD-originated `VoteTransaction`/`TicketPurchase` rows use a
   synthetic buyer email of `"{phone_number}@ussd.vote"`, downstream code
   (`send_ticket_email`, ticket retrieval by phone) specifically checks for
   and skips/handles this sentinel domain.

## 5. Data Model Summary

Core entities defined in `voting/models.py`:

- `Profile` (1:1 with `User`) — `is_approved_organizer` gate.
- `Event` — voting mode (`Pay to Vote` / `Code Voting`), code-voting
  sub-mode (`Standard` / `Student ID`), tie-breaker toggle, theming fields,
  `platform_fee_percentage`, `organizer` FK, and derived helpers
  `get_total_revenue()` / `get_organizer_payout()` (computed on read, not
  persisted or paid out automatically — see §10).
- `Category` — grouping of candidates within an `Event`.
- `Candidate` — auto-generates a unique `nominee_code` (2 letters + 3
  digits) on save if not supplied, used by the USSD flow.
- `VoteTransaction` — the single source of truth for both paid and
  code/ticket-based votes; `status` (`Pending`/`Success`/`Failed`),
  `vote_type` (`Main`/`Tie-Breaker`), `number_of_votes`,
  `paystack_reference` (unique — doubles as an idempotency key even for
  non-Paystack-originated rows such as USSD or code votes, which use
  synthetic prefixes like `USSD_`, `TIE_`, `code_...`).
- `ActivityLog` — free-text audit trail of organizer/admin actions.
- `ProductCategory` / `Product` — simple merch store, no checkout/payment
  wiring visible in `views.py` beyond listing.
- `VotingCode` — single-use code (optionally bound to a `voter_identifier`
  i.e. student ID) for `Code Voting` events.
- `Ticket` / `TicketPurchase` — ticket types per event and purchase records,
  with QR-code check-in fields (`is_checked_in`, `checked_in_at`) and a
  `has_voted` flag used to gate the tie-breaker free-vote flow.

Full field-by-field schema, relationships, and constraints are documented
separately in `docs/DATABASE.md`.

## 6. Third-Party Integrations

### 6.1 Paystack

- **Purpose**: card/mobile-money payment collection for votes and ticket
  purchases (web only).
- **Auth**: `Authorization: Bearer <PAYSTACK_SECRET_KEY>` header on the
  `POST /transaction/initialize` call (`voting/services.py`). Webhook
  authenticity is verified independently via HMAC-SHA512 of the raw request
  body using the same secret key, compared with `hmac.compare_digest`
  (timing-safe) in `views.paystack_webhook`.
- **Env vars**: `PAYSTACK_SECRET_KEY`, `SITE_URL` (used to build the
  `callback_url` passed to Paystack).
- **Failure modes / fallbacks in code**:
  - Network error or non-2xx from Paystack on initialize →
    `_paystack_initialize` logs (`logger.exception`) and returns
    `(None, None)`; callers redirect back to the event/ticket page with no
    transaction row created (silent failure from the voter's perspective —
    no explicit error message on the vote path, though `buy_ticket` does
    show `messages.error`).
  - Webhook signature mismatch → HTTP 400, no state change.
  - Webhook for an unknown reference → `DoesNotExist` is caught and
    ignored; returns HTTP 200 anyway (matches Paystack's expectation that
    webhooks always ack, but means a mismatched reference is silently
    dropped rather than logged).
  - **No webhook retry/idempotency guard beyond the natural
    `Pending → Success` gate** — re-delivery of the same webhook event is
    idempotent because the code only flips status if currently `Pending`.
  - As noted in §4.1, `vote_success` provides an unauthenticated
    fallback path that can also flip `Pending → Success`, bypassing
    Paystack verification entirely.

### 6.2 Africa's Talking (AT)

- **Purpose**: (a) USSD callback endpoint for the feature-phone voting/
  ticket flow (actively used), (b) mobile-money checkout initiation via
  `voting/at_service.py` (present but **not wired into any view or URL** —
  dead code / available-but-unused integration).
- **Auth**: SDK-level initialization — `africastalking.initialize(username=
  settings.AT_USERNAME, api_key=settings.AT_API_KEY)` executed at **module
  import time** in `at_service.py`. Because this runs on import rather than
  lazily, if `AT_API_KEY` is unset/invalid the SDK call itself may still
  succeed at import (the actual API key is validated to Africa's Talking's
  servers only when a call like `mobile_checkout` is made), but any consumer
  importing `at_service` pays this initialization cost even though nothing
  currently calls into it.
- **Env vars**: `AT_USERNAME` (default `'sandbox'`), `AT_API_KEY`.
- **USSD callback auth**: none beyond `@csrf_exempt` — the endpoint trusts
  any POST with the expected fields; there is no verification that the
  request actually originated from Africa's Talking (e.g. no shared-secret
  or IP allowlist check). This is a gap worth addressing before production
  hardening.
- **Failure modes / fallbacks**:
  - `trigger_mobile_money_checkout` wraps the AT SDK call in a bare
    `try/except Exception`, `print()`s the error, and returns `None` — since
    it's unused, this failure path is currently inert, but if wired in it
    would need proper logging instead of `print`.
  - `ussd_callback` itself has no interaction with `at_service` at all; USSD
    votes/tickets are recorded as `Success` unconditionally (see §4.2) — the
    "failure mode" for money collection on USSD is effectively "there is
    none; payment is not actually collected."

### 6.3 Cloudinary

- **Purpose**: durable object storage for all `ImageField` uploads
  (`Event.background_image`, `Event.event_image`, `Candidate.image`,
  `Product.image`, `Ticket.image`) across all environments — `settings.py`
  sets `STORAGES['default']` (and the legacy `DEFAULT_FILE_STORAGE`, kept in
  sync for third-party code that still reads it directly) to
  `cloudinary_storage.storage.MediaCloudinaryStorage` unconditionally (not
  gated behind `DEBUG`), so local dev also uploads to Cloudinary unless
  credentials are absent. Note: this Django version (`Django==6.0.7`) only
  resolves `default_storage` from `STORAGES` — defining only the legacy
  `DEFAULT_FILE_STORAGE` setting silently falls back to Django's built-in
  `FileSystemStorage` instead of Cloudinary, which is a real bug that was
  found and fixed during this review (uploads were landing on local disk and
  404ing/getting wiped on restart). Both settings must be kept present and
  in agreement.
- **Auth**: API key/secret pair read into `CLOUDINARY_STORAGE` dict from
  `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`
  env vars; the `django-cloudinary-storage` backend + `cloudinary` SDK
  handle the actual signed upload requests.
- **Env vars**: `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`,
  `CLOUDINARY_API_SECRET`.
- **Failure modes / fallbacks**: none implemented in application code — if
  credentials are missing/invalid, image upload calls (`ImageField.save()`
  during model `.save()`) will raise at the storage-backend level and
  propagate as an unhandled exception through the view (e.g.
  `create_event`, `add_candidate`, `create_ticket` would 500). There is no
  try/except around image saves, and `MEDIA_URL`/`MEDIA_ROOT` local
  filesystem storage is only used when `DEBUG` is on and the URLconf serves
  it directly (`vote_fund/urls.py`) — this is dead weight in production
  since `STORAGES['default']` always points at Cloudinary regardless of
  `DEBUG`.

### 6.4 Gmail SMTP

- **Purpose**: transactional email — organizer-approval notifications
  (`admin.py: approve_organizers`), new-organizer-registration alerts to
  superusers (`views.register_view`), voting-code retrieval emails
  (`views.retrieve_voting_code`), and e-ticket delivery with a base64
  PNG/JPEG QR-code attachment (`views.send_ticket_email`).
- **Auth**: `EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'`,
  `EMAIL_HOST = 'smtp.gmail.com'`, port 587, `EMAIL_USE_TLS = True`,
  credentials from `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` (a Gmail App
  Password is required since Gmail disallows plain account passwords over
  SMTP for third-party apps).
- **Env vars**: `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`;
  `DEFAULT_FROM_EMAIL` is derived from `EMAIL_HOST_USER` (no separate
  "from" address is configurable).
- **Failure modes / fallbacks**: every call site uses `fail_silently=True`
  (or wraps `.send()` in a bare `try/except: pass`), so an SMTP outage,
  auth failure, or quota limit **never surfaces to the user or the logs** —
  the request completes as if the email were sent. This is a deliberate
  choice to not block the primary flow (e.g. registration, ticket purchase)
  on email delivery, but it means email delivery failures are invisible
  operationally; there is currently no monitoring, retry, or dead-letter
  mechanism for failed sends. All sends are also **synchronous**, executed
  inline in the request/response cycle (see §10 — no task queue).

## 7. Configuration & Environment Variables

All configuration is environment-driven via `python-dotenv` (`load_dotenv()`
at the top of `settings.py`) plus `os.getenv`. Reference: `.env.example`.

| Variable | Used for | Default if unset |
|---|---|---|
| `SECRET_KEY` | Django cryptographic signing | none — required |
| `DEBUG` | Toggles debug mode, security-cookie flags, HSTS opt-in | `'False'` |
| `ALLOWED_HOSTS` | Comma-separated allowed Host headers | `localhost,127.0.0.1,0.0.0.0,flexyvotes.onrender.com` |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated trusted origins (scheme required) | empty |
| `SITE_URL` | Base URL used to build the Paystack `callback_url` | `http://127.0.0.1:8000` |
| `DATABASE_URL` | Postgres connection string (via `dj_database_url.config(conn_max_age=600, ssl_require=True)`) | falls back to local SQLite if unset |
| `PAYSTACK_SECRET_KEY` | Paystack API auth + webhook signature verification | none — required for payments |
| `AT_USERNAME` | Africa's Talking SDK username | `'sandbox'` |
| `AT_API_KEY` | Africa's Talking SDK API key | none |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | Gmail SMTP auth; `EMAIL_HOST_USER` also becomes `DEFAULT_FROM_EMAIL` | none |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | Cloudinary storage credentials | none |
| `DJANGO_LOG_LEVEL` | Root logger level | `'INFO'` |
| `SECURE_SSL_REDIRECT` | (non-DEBUG only) force HTTPS redirect | `'False'` |
| `SECURE_HSTS_SECONDS` | (non-DEBUG only) HSTS max-age; also drives `INCLUDE_SUBDOMAINS`/`PRELOAD` flags (`> 0`) | `'0'` |

Notable settings not driven by env vars: `TIME_ZONE = 'Africa/Accra'`,
`SESSION_COOKIE_SAMESITE = 'Lax'`, `X_FRAME_OPTIONS = 'DENY'`,
`SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` are tied directly to
`not DEBUG` (not independently configurable).

## 8. Caching & Rate Limiting

`CACHES['default']` is `django.core.cache.backends.locmem.LocMemCache`
(`settings.py`). It's used purely for basic IP-based rate limiting, not for
general query/page caching:

- `login_view`: keys `login_attempts_{ip}`, blocks after 5 failed attempts
  within a 60-second TTL window (counter reset on successful login).
- `register_view`: keys `register_attempts_{ip}`, blocks after 3 attempts
  within 60 seconds (counter increments on every POST, success or not).
- `send_ticket_email`: keys `send_ticket_email_{ip}`, blocks after 10 calls
  within 60 seconds — protects the open (`@csrf_exempt`, unauthenticated)
  email-sending endpoint from being used as a spam relay.

**Known limitation**: `LocMemCache` is per-process, in-memory, and not
shared across workers. Gunicorn typically runs multiple worker processes
(and in a containerized/multi-instance AWS deployment, multiple containers
behind a load balancer), so:
- Rate-limit counters are tracked independently per worker/container — an
  attacker can trivially get `N × workers × instances` attempts instead of
  `N`, defeating the intended limit.
- Nothing is shared between deploys/restarts (cache is wiped on every
  process restart, which also resets all rate limits).

This should move to a shared backend (Redis or Memcached) before scaling to
multiple gunicorn workers or multiple container instances — this is the
single most important scaling-correctness fix identified in this review.

## 9. Logging

`settings.py` defines a minimal `LOGGING` dict:

- Single `console` handler (`logging.StreamHandler`) — no file handler, no
  rotation, no external log-shipping configured in code (any Cloud
  aggregation would rely on stdout/stderr capture by the hosting platform,
  e.g. CloudWatch Logs when containerized on AWS).
  - `root` logger level is controlled by `DJANGO_LOG_LEVEL` (default
    `INFO`).
- `django.request` logger explicitly set to `ERROR` with
  `propagate: False` — this suppresses Django's default noisy per-request
  warning logs (e.g. 404s) at anything below ERROR, and stops them
  double-logging via the root logger.
- Application code only explicitly logs from `voting/services.py`
  (`logger = logging.getLogger(__name__)`) — a `logger.exception` on
  Paystack request failures and a `logger.warning` when Paystack rejects an
  initialize call. No other module (`views.py`, `at_service.py`,
  `admin.py`) uses the logging framework — `at_service.py` uses a bare
  `print()` for its error path, and most `except Exception: pass` blocks in
  `views.py`/`admin.py` swallow errors with no log record at all (e.g. the
  email-sending `try/except` blocks). This makes silent email failures and
  swallowed exceptions effectively invisible in production logs.
- Gunicorn is started with `--log-file -` (per `Procfile`), sending
  gunicorn's own access/error logs to stdout, which composes reasonably
  with a containerized deployment where the platform captures container
  stdout.

## 10. Scalability Considerations & Known Bottlenecks

- **SQLite as the effective default**: `DATABASES['default']` is SQLite
  unless `DATABASE_URL` is present in the environment. SQLite does not
  support concurrent writers well and is unsuitable for any real deployment
  with multiple gunicorn workers hitting the same file — production must
  always set `DATABASE_URL` to Postgres (the code does support this via
  `dj_database_url.config(conn_max_age=600, ssl_require=True)`, which also
  gives Postgres connection pooling via persistent connections).
- **LocMemCache per-process rate limiting** (detailed in §8) — breaks down
  under multi-worker/multi-instance deployment; needs Redis/Memcached.
- **Vote counts computed on every request**: `event_detail`,
  `live_vote_counts` (polled — likely on an interval from the frontend for
  "live" updates), and `event_analytics` all run `Sum(...)` aggregate
  queries across `VoteTransaction` on every hit rather than maintaining a
  denormalized counter or a cached aggregate. For high-traffic events with
  many transactions this is the most likely per-request DB bottleneck,
  especially since `live_vote_counts` is designed to be polled repeatedly.
  There are no DB indexes declared beyond Django's implicit FK indexes and
  the `unique=True` constraints on `paystack_reference`/`code`/
  `nominee_code` — no explicit index on `VoteTransaction.status` or
  `(candidate, status, vote_type)`, which is the exact filter combination
  used by every vote-count query.
- **No pagination anywhere**: every list-returning view
  (`home`, `dashboard`, `store_view`, `manage_store`, `event_guestlist`,
  `tickets_view`'s event listing, `download_codes`/`download_guestlist` CSV
  exports) loads the entire queryset with `.all()`/`.filter()` and no
  `Paginator`. This will degrade linearly as `Event`, `Product`,
  `TicketPurchase`, and `VotingCode` tables grow.
- **Synchronous email, no task queue**: there is no Celery (or any async
  task runner) in `requirements.txt` or `settings.py`. Every
  `send_mail`/`EmailMultiAlternatives`/`EmailMessage.send()` call — approve-
  organizer notification, registration alert, voting-code retrieval email,
  e-ticket email with QR attachment — executes synchronously inside the
  request/response cycle on the same gunicorn worker. Under Gmail SMTP
  latency or an SMTP outage, this directly slows down (or, without
  `fail_silently`, could fail) unrelated user-facing requests, and ties up a
  worker thread/process for the duration of the SMTP round trip. Moving
  email dispatch to Celery + Redis/SQS (or at minimum Django's
  `send_mail` with a queued backend) is recommended before scaling traffic.
- **Single WSGI process model, ASGI unused**: `asgi.py` exists but nothing
  in the Procfile or settings uses it; there's no `Channels`/websocket
  usage. `live_vote_counts` implements "live" updates via polling a
  JSON endpoint rather than websockets/SSE, which is consistent with a pure
  WSGI deployment but means live-count freshness is bounded by client poll
  interval and adds recurring read load per open page (see aggregate-query
  bottleneck above).
- **No CDN/cache-control strategy beyond Whitenoise** for static assets
  (Whitenoise's `CompressedManifestStaticFilesStorage` does provide
  cache-busted, gzip/brotli-compressed static files, which is good for a
  single-container deployment, but there's no CloudFront/CDN layer
  described in the current code/config).

## 11. Known Technical Debt / TODOs

1. **Africa's Talking mobile-money integration is unused.**
   `voting/at_service.py`'s `trigger_mobile_money_checkout` is fully
   implemented but never imported/called from `views.py` or any URL. USSD
   votes and USSD ticket purchases are instead recorded as `Success`
   unconditionally with no real payment collection (see §4.2). Either wire
   this in to actually charge mobile-money users on USSD, or remove the
   dead code.
2. **Unauthenticated "success" fallback in `vote_success`.** The
   `GET /vote/success/` view flips a `Pending` `VoteTransaction` to
   `Success` purely from a client-supplied `?reference=`, without
   verifying against Paystack. Combined with the webhook being the "real"
   confirmation path, this fallback (labeled in-code as being for local
   testing) is a live bypass in production — anyone who learns/guesses a
   pending reference can mark it paid. Should call Paystack's
   `GET /transaction/verify/:reference` before flipping status, or remove
   the fallback and rely solely on the webhook (showing a "processing,
   check back" state instead).
3. **No USSD callback origin verification.** `ussd_callback` is
   `@csrf_exempt` (necessarily, since AT doesn't send a CSRF token) but has
   no compensating check (shared secret, IP allowlist) that the request
   actually came from Africa's Talking.
4. **No task queue** — all email sends are synchronous in the request path
   (see §10). Recommend Celery + Redis/SQS, or at minimum an async email
   backend.
5. **No pagination** on any list view (`home`, `dashboard`, `store_view`,
   `manage_store`, guestlist, ticket listings) — will degrate as data
   volume grows.
6. **No automated payout mechanism.** `Event.get_organizer_payout()` only
   *computes* the organizer's share (`total_revenue - platform_fee`) for
   display (e.g. in analytics); there is no integration that actually
   transfers funds to organizers (e.g. via Paystack Transfers) — this is
   presumably a manual, off-platform process today.
7. **Per-process rate limiting via `LocMemCache`** — not correct once
   deployed with more than one worker/instance (see §8); needs a shared
   cache backend.
8. **Duplicated USSD route.** `/ussd/callback/` is registered both directly
   in `vote_fund/urls.py` and again via the included `voting/urls.py`,
   pointing at the same view — redundant and worth removing one.
9. **Silent, unlogged failures.** Widespread `except Exception: pass` (or
   `fail_silently=True`) around email sends and a few other operations
   means operational issues (SMTP outages, Cloudinary errors) leave no log
   trace. Combined with the minimal `LOGGING` config (console-only, no
   structured logging/error tracking service wired in), diagnosing
   production issues after the fact will be difficult. Consider adding
   Sentry (or similar) plus turning silent excepts into at least
   `logger.warning`/`logger.exception` calls.
10. **`ASGI` scaffold unused** — `vote_fund/asgi.py` exists from Django's
    project template but nothing in the deployment (`Procfile`, gunicorn
    invocation) uses it. Harmless, but dead weight/config drift risk if
    someone assumes async support exists.
11. **No image-upload error handling.** Cloudinary credential or network
    failures during an `ImageField` save (`create_event`, `add_candidate`,
    `create_ticket`, etc.) will raise an unhandled exception straight
    through the view (500) rather than a friendly form error.
12. **Merch store has no checkout flow.** `Product`/`ProductCategory` and
    `store_view` only support browsing; there's no visible purchase/payment
    integration for the store in `views.py`, unlike votes and tickets.
