# Architecture

This page shows how FlexyVotes is put together: its components, how requests and data move
between them, and where the trust boundaries sit. Design decisions and their reasons are in
[TRD.md](TRD.md). The schema is in [DATABASE.md](DATABASE.md).

## 1. System context

```
                    ┌───────────────────────── AWS ──────────────────────────────┐
 Voters / fans ──▶  │  ALB or nginx (TLS) ──▶ web (Gunicorn, N replicas)          │
 Staff (console)    │                          │        │                         │
 API clients        │                          │        ├──▶ Redis (cache, rate   │
 Auditors           │                          │        │     limits, broker)     │
                    │                          ▼        ▼                         │
                    │                    PostgreSQL ◀── worker (Celery, N)        │
                    │                    (RDS, PgBouncer)  beat (Celery, exactly 1)
                    │                          ▲                                  │
                    │                    KMS (key encryption key), S3 (backups)   │
                    └──────────────────────────┼──────────────────────────────────┘
                                               │
  External: Paystack (payments, webhooks) · Africa's Talking (SMS, USSD) · WhatsApp Cloud API
            SMTP · Google / Microsoft OIDC · customer LDAP · Cloudinary (public media)
            Sentry · OTLP collector · Prometheus
```

There are four process types, all built from the same Docker image. The entrypoint argument
selects which one runs (`docker-entrypoint.sh`):

| Process | Command | Scaling |
|---|---|---|
| `web` | Gunicorn (`gthread`), serves HTML, API, webhooks, health and metrics | Horizontal, stateless |
| `worker` | Celery worker on queues `default`, `notifications`, `payments` | Horizontal |
| `beat` | Celery beat scheduler | **Exactly one** |
| `migrate` | `manage.py migrate` | One-off per release |

## 2. Django apps and their dependencies

```
              ┌──────────┐
              │   api    │  REST v1 (django-ninja)
              └────┬─────┘
   ┌───────────────┼───────────────┬──────────────┬──────────────┐
   ▼               ▼               ▼              ▼              ▼
┌────────┐  ┌───────────┐  ┌──────────┐  ┌───────────────┐ ┌─────────┐
│ voting │◀─│ elections │  │ payments │─▶│     fraud     │ │ billing │
└───┬────┘  └─────┬─────┘  └────┬─────┘  └───────────────┘ └────┬────┘
    │             │             │                               │
    └─────────────┴──────┬──────┴───────────────────────────────┘
                         ▼
                ┌─────────────────┐       ┌───────────────┐
                │      core       │◀──────│ notifications │
                └─────────────────┘       └───────────────┘
```

- **core** depends on no other app. It provides the crypto primitives, envelope encryption,
  RBAC, the audit chain, authentication (lockout, MFA, passkeys, SSO, LDAP, OTP), rate limits,
  idempotency, the outbound HTTP allow-list, middleware, tenancy and the staff console shell.
- **voting** owns `Event` (the election), `Category` (a position), `Candidate`, the paid-vote
  ledger (`VoteTransaction`), tickets and the store. It also serves the public event pages,
  the organizer pages and USSD.
- **elections** holds the institutional engine: voters, constituencies, eligibility,
  authorizations, ballots, keys, tally, results, verification and integrity controls. It
  never imports `payments`.
- **payments** turns a Paystack result into credited votes, once, and asks **fraud** for a
  risk decision first.
- **billing** meters organizations and invoices them. Invoices are paid through `payments`.
- **notifications** is an outbox. Any app enqueues a message. Delivery happens after the
  transaction commits.

## 3. Request pipeline

Middleware runs in this order (see `vote_fund/settings.py`):

1. `CorrelationIdMiddleware` reads or creates `X-Request-ID`, which is attached to logs,
   audit events and Sentry.
2. `MetricsMiddleware` records Prometheus request counters and latency.
3. `SecurityMiddleware`, then `WhiteNoiseMiddleware` for static files.
4. `BackpressureMiddleware` returns 503 with `Retry-After` to POSTs under `/api/v1/`,
   `/payments/` and `/e/` when the total Celery queue depth exceeds
   `QUEUE_BACKPRESSURE_THRESHOLD`. Payment webhooks are exempt, so confirmations are never
   shed.
5. Sessions, `LocaleMiddleware`, `CommonMiddleware`.
6. `ApiCorsMiddleware` handles CORS for `/api/` only, from `API_CORS_ALLOWED_ORIGINS`.
7. CSRF, then authentication.
8. `SessionSecurityMiddleware` tracks the session (`UserSession`), handles revocation, idle
   expiry and enforcing MFA for staff.
9. Messages, then `X-Frame-Options`.
10. `UserPreferencesMiddleware` applies accessibility mode, timezone and the device cookie.
11. `SecurityHeadersMiddleware` sets the nonce-based CSP, Permissions-Policy, COOP/CORP and
    friends.

## 4. Key flows

### 4.1 Institutional vote (secret ballot)

```
Voter                    web (elections.views_voter)              DB
  │ GET /e/<id>/vote/            │                                  │
  │ POST credentials ───────────▶│ voter_auth: code | OTP | SSO | LDAP | account
  │                              │ rate limits per IP and per identifier
  │                              │ issue_authorization():           │
  │                              │   lock Voter row, check eligibility, ballot style
  │                              │   revoke older unused authorizations
  │                              │   VoteAuthorization(token_hash, style, expiry)  ──▶ │
  │◀── session holds raw token ──│ audit VOTER_BALLOT_ISSUED (no ballot data)         │
  │ choose → review (submission_id, explicit confirm tick)                             │
  │ POST cast ──────────────────▶│ cast_ballot():  (one transaction)                  │
  │                              │   SELECT … FOR UPDATE the authorization by hash    │
  │                              │   CONSUMED? → "already voted";  expired? → reject  │
  │                              │   validate selections against ballot rules         │
  │                              │   plaintext = canonical JSON + random nonce        │
  │                              │   ciphertext = ECIES seal(election public key,     │
  │                              │                 aad = election:style_hash)         │
  │                              │   Ballot(ciphertext, tracker=sha256(ciphertext))──▶│
  │                              │   authorization → CONSUMED; voter → VOTED          │
  │                              │   audit VOTER_VOTED (participation only)           │
  │◀── receipt with tracker ─────│ on commit: confirmation email (without tracker)   │
```

What breaks the link between voter and ballot:

- A `Ballot` row has a random UUID, the ciphertext, the tracker, the style hash and,
  optionally, the constituency. It has **no voter, no authorization and no timestamp**.
- `VoteAuthorization` stores only `sha256(token)`. The raw token lives only in the voter's
  session and is discarded after casting.
- The audit log records *that* a voter voted, never the tracker or the choices.
- Plaintext choices exist only in memory during casting and tallying.

### 4.2 Tally, certify, publish

```
CLOSED ──start_tally──▶ TALLYING
   run_tally():  unseal every ballot (system key, or k-of-n trustee shares)
                 invalid or tampered ciphertext → counted as invalid, never silently dropped
                 tally_position() per position (plurality, approval, score, IRV/STV, referendum)
                 ElectionResult(data, result_hash, bulletin_root = Merkle root of trackers)
   approve_and_certify(): a different person from the tallier (separation of duties)
                 ResultCertification(payload, Ed25519 signature) ──▶ CERTIFIED
   publish ──▶ PUBLISHED: public results page, verification page, bundle.json
```

Recounts re-run the tally and compare it with the certified result. Decertifying needs dual
approval.

### 4.3 Paid vote

```
Fan ──POST /e/<id>/pay/<candidate>/──▶ payments.service.initiate_vote_payment()
        quote (bundle / custom count, discount, limits) → Payment(INITIALIZED, idempotency key)
        Paystack initialize → redirect to checkout
Paystack ──webhook (HMAC-SHA512)──▶ /payments/webhook/   (also /webhook/paystack/, /api/v1/webhooks/paystack)
        dedupe by payload hash (WebhookEvent), then process inline;
        on failure → 500, so Paystack retries (and reconciliation catches anything left)
Fan ──callback──▶ /payments/callback/?reference=… → server-side verify (never trusts the redirect)
Both paths ──▶ apply_gateway_result()   (single idempotent entry point)
        lock Payment; check amount and currency match exactly
        fraud.assess() → ALLOW / MONITOR (credit) · CHALLENGE / HOLD (hold, analyst reviews)
        VoteTransaction(payment = OneToOne) → votes credited exactly once
Every 15 min: reconciliation compares recent payments with Paystack and records discrepancies.
Every 10 min: abandoned checkouts expire.
```

USSD works the same way. It starts a direct mobile-money charge through Paystack, and
nothing is credited until Paystack confirms the charge.

### 4.4 Configuration changes during an election

`elections.lifecycle.edit_policy()` decides whether a scope (config, ballot, candidates,
voters, candidate profile) may change, based on the current state and any freezes:

- **Draft:** freely editable.
- **Review:** locked until the election is withdrawn or returned.
- **Approved / Scheduled:** editing returns the election to Draft for re-approval.
- **Open / Paused:** only voters and candidate profiles change (plus candidates and ballot
  for paid events).
- **Closed and later:** read-only.

Each approval stores a signed configuration snapshot (`ElectionConfigSnapshot`), so a change
after approval is detectable.

## 5. Background jobs

| Schedule | Task | Purpose |
|---|---|---|
| every 60 s | `elections.tasks.lifecycle_tick` | Auto-open scheduled elections; auto-close at end date |
| hourly | `elections.tasks.send_voting_reminders` | Reminder notifications to voters who haven't voted |
| every 15 min | `payments.tasks.reconcile_recent` | Paystack reconciliation |
| every 10 min | `payments.tasks.expire_abandoned` | Expire stale checkouts |
| every 5 min | `fraud.tasks.anomaly_scan` | Vote bursts, coordinated campaigns, unusual geography |
| every 10 min | `notifications.tasks.retry_failed` | Retry failed deliveries with backoff |
| every 6 h | `core.tasks.verify_audit_chains` | Verify every audit hash chain; alert on breaks |
| daily 01:15 | `billing.tasks.generate_due_invoices` | Subscription invoices |
| daily 02:30 | `core.tasks.housekeeping` | Purge expired idempotency records, old OTPs and 90-day-idle sessions; expire unused ballot authorizations |

Tasks named `notifications.*` and `payments.*` go to their own queues, so a backlog of
emails cannot delay payment processing.

## 6. Trust boundaries

| Boundary | Control |
|---|---|
| Internet → app | TLS at ALB or nginx; HSTS; `TRUSTED_PROXY_COUNT` decides which `X-Forwarded-For` hop is the client |
| Browser → app | Session cookies are `Secure`, `HttpOnly` and `SameSite=Lax`; CSRF on every form; nonce CSP with no inline handlers |
| Paystack → app | HMAC-SHA512 signature, dedupe, amount check, server-side verification |
| Africa's Talking → app (USSD) | `USSD_CALLBACK_TOKEN` and/or `USSD_ALLOWED_IPS` |
| App → outside | `core.http.validate_url` allow-list and public-IP check (no SSRF), timeouts, circuit breakers |
| App → database | Least-privilege role; append-only triggers on audit, ballot, evidence and payment-event tables |
| Data at rest | AES-GCM envelope encryption of PII and secrets; KEK in KMS or env; blind indexes for lookups |
| Election officials → ballots | Officials can't see individual ballots; trustee custody needs k of n shares to decrypt |

## 7. Scaling notes

- Web and worker are stateless, so they scale out. Sessions, cache, rate limits and locks
  live in Redis and PostgreSQL.
- PgBouncer runs in transaction mode, so server-side cursors are disabled
  (`DB_DISABLE_SERVER_SIDE_CURSORS=True`) and connections aren't kept (`DB_CONN_MAX_AGE=0`).
- A read replica (`DATABASE_REPLICA_URL`) serves read-heavy reporting through
  `core.db_router`. Voting and payment writes always go to the primary.
- Prometheus runs in multiprocess mode, so `/metrics` aggregates all Gunicorn workers.
- Hot paths (casting, crediting) take one row lock and do one insert each. The concurrency
  tests in `elections/tests/test_concurrency.py` run them under real parallel load on
  PostgreSQL.
