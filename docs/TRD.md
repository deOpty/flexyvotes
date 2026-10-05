# Technical Requirements & Design (TRD)

This document explains how FlexyVotes meets the requirements in [PRD.md](PRD.md) and why it
was built that way. For component diagrams see [ARCHITECTURE.md](ARCHITECTURE.md); for the
schema see [DATABASE.md](DATABASE.md).

## 1. Constraints and decisions

| Decision | Reason |
|---|---|
| Django monolith split into apps (`core`, `voting`, `elections`, `payments`, `fraud`, `notifications`, `billing`, `api`) | One deployable unit and one database transaction across ledger and audit, with clear module boundaries. The election engine never imports payments. |
| PostgreSQL in production | Row locks (`SELECT … FOR UPDATE`), partial unique constraints, triggers and PITR. SQLite works for development, and the tests that need PostgreSQL skip themselves on SQLite. |
| Server-rendered HTML plus a REST API | The voting flow must work without JavaScript on low-end phones. The API (django-ninja) serves integrations and future apps. |
| Celery + Redis | Notifications, reconciliation, scheduled transitions and scans run off the request path. Without a broker, tasks run inline (eager), so development needs no worker. |
| `Event` stays the election model | Existing data, URLs and organizer workflows keep working. Migrations 0036–0038 moved legacy data onto the new model (statuses, organizations, roles, encrypted voter rolls). |

## 2. Election engine

### 2.1 Lifecycle (`elections/lifecycle.py`)

The lifecycle is a table of `Action(name, sources, target, permission, guard, effect)`.
`transition()`:
1. checks the permission;
2. locks the `Event` row;
3. checks that the source state is allowed;
4. runs the guard, then the effect;
5. writes an audit event, all in one transaction.

| Action | From → To | Permission | Guard / effect |
|---|---|---|---|
| submit | DRAFT → REVIEW | `election.submit` | `configuration_problems()` must be empty |
| withdraw / reject | REVIEW → DRAFT | submit / review | |
| approve | REVIEW → APPROVED | `election.review` | Approver ≠ submitter; stores a signed config snapshot |
| schedule / unschedule | APPROVED ⇄ SCHEDULED | `election.publish` | End date in the future; creates election keys |
| open | SCHEDULED → OPEN | `election.publish` | End date not passed |
| pause / resume | OPEN ⇄ PAUSED | `election.pause` | Can't resume after the end date |
| close | OPEN/PAUSED → CLOSED | `election.close` | |
| start_tally | CLOSED → TALLYING | `results.tally` | |
| certify | TALLYING → CERTIFIED | `results.certify` | System-only: done by `approve_and_certify()` |
| publish | CERTIFIED → PUBLISHED | `results.publish` | |
| archive | PUBLISHED/CLOSED/DRAFT → ARCHIVED | `election.archive` | No legal hold; no open disputes |
| decertify, reopen_voting | — | — | System-only, run through dual approval |

`lifecycle_tick` (every 60 s) opens SCHEDULED elections whose start has passed and closes
OPEN or PAUSED ones whose end has passed. It uses `skip_guard` and `system=True`.

`edit_policy(event, scope)` decides whether a scope can change in the current state; see
[ARCHITECTURE.md §4.4](ARCHITECTURE.md#44-configuration-changes-during-an-election). Every
editing view calls `require_editable()`, which can also send an approved election back to
DRAFT.

### 2.2 Ballot rules (`elections/ballot.py`)

| Type | Valid selection | Tally |
|---|---|---|
| SINGLE / FPTP | Exactly one candidate (or abstain) | Plurality |
| MULTIPLE | `min_select`..`max_select` distinct candidates | Plurality, `seats` winners |
| APPROVAL | Any subset up to `max_select` | Plurality over approvals |
| RANKED | Distinct ranks 1..k, no gaps | IRV (1 seat, majority of continuing ballots) or STV (several seats) |
| SCORE | Integer 0..`max_score` per candidate | Sum of scores |
| REFERENDUM | YES or NO | Passes if YES% of valid votes > `referendum_threshold` |

`validate_ballot()` runs on the server for every cast, whatever the client sent. A position
outside the voter's ballot style, an unknown candidate, a duplicate, or a count out of range
rejects the whole ballot.

### 2.3 Tally algorithms (`elections/tally.py`)

The tally functions are pure: no database access, and deterministic.

- **Ties:** a tie that decides a seat is reported in `ties` and never broken silently.
- **Forced tie-breaks:** IRV and STV eliminations must continue, so they break ties with a
  documented lot: SHA-256 of `election_id:candidate_id`. Anyone re-running the count gets
  the same result, and every lot is recorded in `tie_breaks`.
- **STV quota:** Droop, computed exactly with `Fraction` as valid ÷ (seats + 1). A candidate
  is elected on reaching the quota.
- **STV surplus:** transferred with Gregory fractional weights.
- **IRV:** needs more than half of the continuing ballots.
- **Invalid ballots:** a ballot whose ciphertext fails to decrypt or validate counts as
  invalid. It is reported, and never dropped silently.

### 2.4 Results (`elections/results.py`)

1. **Tally.** `run_tally()` decrypts every ballot of the election, tallies each position
   (respecting each ballot's style) and stores an `ElectionResult` with:
   - `result_hash`: SHA-256 of the canonical result JSON;
   - `bulletin_root`: Merkle root over the sorted ballot trackers.
2. **Certify.** `approve_and_certify()` needs a different user from the tallier. It signs
   the canonical JSON of the following with the platform Ed25519 key, producing a
   `ResultCertification`:
   - election id and title, organization;
   - `result_hash` and `bulletin_root`;
   - ballots counted, votes cast, eligible voters;
   - the latest config-snapshot hash;
   - the ballot-key fingerprint;
   - `certified_at`.
3. **Recount.** `recount()` re-tallies and compares the outcome with the certified result.
   Results are `matches` or a list of differences.
4. **Publish.** `public_results()` respects `results_visibility`, and hides
   constituency breakdowns smaller than `min_anonymity_set`.
5. **Verify.** `verification_bundle()` and `verify_bundle()` produce and check the public
   bundle. The offline verifier `tools/verify_election.py` re-checks:
   - the certification signature;
   - the result hash;
   - the Merkle root from the trackers.

## 3. Ballot secrecy and cryptography

### 3.1 Unlinkability

| Record | Knows the voter? | Knows the choices? |
|---|---|---|
| `Voter` | yes | no |
| `VoteAuthorization` | yes (FK) | no; holds only `HMAC(token)` and the ballot style |
| `Ballot` | **no** (random UUID, no FK, no timestamp, no token) | only as ciphertext |
| `AuditEvent VOTER_VOTED` | yes | no; the tracker is deliberately not logged |
| Voter's session | yes | raw token, removed after casting |

`cast_ballot()` locks the authorization by token hash. In one transaction it checks the
state, validates the ballot, seals it and inserts the `Ballot`, then marks the authorization
CONSUMED and the voter VOTED. Before sealing, the plaintext is padded with a 128-bit random
nonce, so identical choices produce unrelated ciphertexts and trackers.

`record_constituency_on_ballot` (off by default) stores the constituency on the ballot so
results can be broken down by constituency. Small groups stay hidden by `min_anonymity_set`.

**Residual risk.** A database superuser can still compare physical insertion order (or WAL)
with `Voter.voted_at`. This is mitigated by database access controls and audit, not by
cryptography. See [SECURITY.md](SECURITY.md#2-threat-model).

### 3.2 Primitives (`core/crypto.py`)

| Use | Algorithm |
|---|---|
| Field encryption (PII, secrets) | AES-256-GCM with per-field AAD `app.model.field`; format `fv1$<data_key_id>$<base64(nonce‖ct)>` |
| Data keys | 256-bit, stored wrapped in `DataKey`; wrapped by a KEK from `FIELD_ENCRYPTION_KEYS` (AES-GCM) or AWS KMS |
| Blind indexes (lookup without decrypting) | HMAC-SHA256 with `BLIND_INDEX_KEY`, input normalized and scoped by purpose |
| Tokens, OTPs, access codes, recovery codes, device IDs | HMAC-SHA256 keyed by `SECRET_KEY`, compared in constant time |
| Ballot sealing | ECIES: ephemeral X25519 → HKDF-SHA256 → AES-256-GCM; AAD = `election_id:style_hash`; tracker = SHA-256 of the sealed blob |
| Election key custody | SYSTEM: the private key is envelope-encrypted. TRUSTEES: Shamir k-of-n over the prime field GF(2^521 − 1), with a SHA-256 checksum so a wrong combination is detected |
| Signatures | Ed25519 platform key (`SIGNING_PRIVATE_KEY`); public key at `/.well-known/flexyvotes-signing-key.json`; older keys trusted through `SIGNING_PREVIOUS_PUBLIC_KEYS` |
| Bulletin board | Binary Merkle tree (SHA-256) with inclusion proofs per tracker |
| Key backups | scrypt-derived key, then AES-GCM (`manage.py keys backup`) |

**Development fallbacks.** Without dedicated keys, the KEK, signing key and blind-index key
are derived from `SECRET_KEY` with HKDF. `manage.py check --deploy` warns about this
(`flexyvotes.W001`–`W003`). The derived KEK always remains available for **unwrapping**, so
data written before real keys were configured stays readable and moves onto the new KEK with
`manage.py keys rewrap`. Data keys remember which provider wrapped them (local or KMS), so
switching to KMS also works. The procedure is in
[OPERATIONS.md](OPERATIONS.md#key-management).

### 3.3 Audit chain (`core/audit.py`)

- **Chains:** one per organization (`org:<id>`) and one for the platform.
- **Hashing:** each event's hash is
  `SHA-256(prev_hash ‖ canonical_json(hashed fields))`. The hashed fields include sequence,
  type, actor, target, summary, changes, metadata, result, IP and timestamp.
- **Appending:** locks the chain's `AuditChainHead` row, so concurrent appends can't fork
  the chain. A concurrency test checks this.
- **Append-only:** the ORM queryset refuses update and delete. On PostgreSQL, triggers block
  `UPDATE` and `DELETE` on `core_auditevent`, `elections_ballot`, `elections_evidenceitem`
  and `payments_paymentevent`.
- **Verification:** `verify_chain()` and `verify_all()` detect modified, deleted or
  reordered entries. They run every 6 hours and on `manage.py verify_integrity`.

## 4. Authentication

| Mechanism | Implementation |
|---|---|
| Passwords | Argon2id (`PASSWORD_HASHERS`), Django validators (min length 10, common, numeric, similarity) |
| Lockout | `LOGIN_MAX_FAILURES` failures → locked for `LOGIN_LOCKOUT_SECONDS`; plus per-IP rate limits |
| TOTP | `pyotp`; secret encrypted; each time step accepted only once (replay protection); 10 recovery codes stored as HMACs |
| Passkeys | WebAuthn (`webauthn` 3.x), resident or roaming; sign-count checked |
| Step-up | A new device or risky sign-in → email OTP; staff with `ENFORCE_STAFF_MFA` must enroll MFA |
| SSO | OIDC authorization code with PKCE, `state` and `nonce`, JWKS signature check, `iss`/`aud`/`exp`; Google, Microsoft, or per-organization config |
| LDAP | `ldap3` bind as the user over LDAPS/StartTLS; per-organization config (encrypted) |
| Voter OTP | 6 digits, 10-minute expiry, 5 attempts, issue rate limited; sent to the address on the roll only |
| Sessions | `UserSession` per login; list, revoke and sign-out-others; revoked sessions are refused by middleware |
| API tokens | Random prefix + secret; only the SHA-256 is stored; optional expiry; revocable |

## 5. Payments (`payments/`)

- **State machine:** `ALLOWED_TRANSITIONS` in `payments/service.py`. Every transition writes
  an append-only `PaymentEvent`.

  ```
  INITIALIZED → PENDING | SUCCESS | FAILED | ABANDONED
  PENDING → SUCCESS | FAILED | ABANDONED      ABANDONED → SUCCESS | FAILED
  FAILED → SUCCESS                            SUCCESS → REFUNDED | PARTIALLY_REFUNDED | REVERSED | DISPUTED
  PARTIALLY_REFUNDED → REFUNDED | DISPUTED | REVERSED     DISPUTED → SUCCESS | REVERSED | REFUNDED
  ```

- **Single entry point:** `apply_gateway_result(reference, data, source)` handles webhooks,
  callbacks and reconciliation. It:
  1. locks the payment;
  2. requires the gateway amount (in pesewas) and currency to equal the quote exactly;
  3. runs `fraud.assess()`;
  4. credits votes once. `VoteTransaction.payment` is a OneToOne, so a second credit is
     impossible even under concurrency.
- **Quotes:** price per vote comes from the position, else the event. Bundles add bonus
  votes. Discount codes check window, minimum amount, total and per-payer redemptions. Limits
  cover min/max per transaction, max votes per voter and max spend per voter (counted by
  blind index of the payer's email or phone).
- **Webhooks:** HMAC-SHA512 over the raw body, compared in constant time. Then a dedupe on
  the SHA-256 of the payload. Events: `charge.success`, `charge.failed`, `refund.*`,
  `charge.dispute.*`.
- **Refunds:** a refund above `REFUND_DUAL_APPROVAL_THRESHOLD` becomes an `ApprovalRequest`.
  If `reverse_votes` is set, a proportional number of votes is reversed.
- **Chargebacks:** reverse the credited votes and raise a fraud event with weight 100.
- **Reconciliation:** lists the gateway's transactions for the window, compares them with
  local payments, records every mismatch as a `ReconciliationItem`, and fixes the safe ones
  automatically (a missed success, for example).
- **Development simulator:** `PAYMENTS_FAKE_GATEWAY` is allowed only with `DEBUG=True`.

## 6. Fraud engine (`fraud/engine.py`)

Signals are added to the score, capped at 100. A decision comes from thresholds that can be
configured.

| Score | Decision | Effect |
|---|---|---|
| 0–30 | ALLOW | Credit |
| 31–60 | MONITOR | Credit and flag |
| 61–80 | CHALLENGE | Hold; the analyst can verify the payer |
| 81–100 | HOLD | Hold; not credited until an analyst approves |

| Signal | Weight |
|---|---|
| Blocklisted IP / device / email / card / phone | 60 |
| Blocklisted email domain | 60 |
| Tor / VPN / proxy range (anonymizer) | 25 |
| Open-proxy headers | 10 |
| Disposable email domain | 35 |
| Missing user agent | 15 |
| Automation user agent | 30 |
| IP velocity (10 min), high / normal | 35 / 20 |
| Device velocity | 20 |
| Email velocity | 15 |
| Failed payments in the last hour, repeated / some | 25 / 15 |
| One device used by many payer emails (account farm) | 25 |
| One IP used by many payer emails | 15 |
| Previous chargeback | 50 |
| One card used by many emails in 24 h | 30 |
| Card over the event's vote limit | 85 |
| Unexpected country | 20 |
| High amount | 10 |
| Burst of payment attempts for one candidate | 15 |

The anomaly scan runs every 5 minutes and raises alerts for vote bursts (50), coordinated
campaigns (55) and unusual geography (45).

## 7. Idempotency, rate limiting and concurrency

- **Idempotency.** `IdempotencyRecord(scope, key, request_hash, state, response)` with a
  unique `(scope, key)`.
  - `begin()` claims the key.
  - Replaying the same key with the same request returns the stored response.
  - Replaying it with a *different* body returns 422.
  - The API reads the `Idempotency-Key` header; payment creation also stores it on
    `Payment.idempotency_key`, which is unique.
- **Rate limits.** These are fixed-window counters in the cache. With `REDIS_URL` set they
  are shared across all replicas. The main limits:

  | Limit | Window |
  |---|---|
  | login | 10 / min / IP |
  | voter login | `VOTER_LOGIN_PER_IP_PER_MIN` per IP (default 120, campus-NAT friendly), plus 10 / 10 min per identifier when one is given |
  | voter OTP entry | `VOTER_OTP_PER_IP_PER_5MIN` per IP (default 100); each OTP allows 5 attempts |
  | OTP issue | 6 per subject per hour, plus a 60 s resend cooldown |
  | payments | 20 / min / IP |
  | registration | 5 / 5 min |
  | dispute filing and tracker lookups | rate limited |
  | API | per principal and scope |

  Exceeding a limit returns 429 with `Retry-After`.
- **Concurrency.** Every hot path takes one row lock: the ballot authorization, the voter,
  the payment, and the audit chain head. Unique constraints are the last line of defence:
  one consumed authorization per voter, one `VoteTransaction` per payment, one idempotency
  key per payment.

## 8. Notifications (`notifications/`)

`notify(channel, template, recipient, context, dedupe_key=…)` writes a `Notification` with
the recipient and context encrypted. The flow:

1. After commit, Celery delivers it on the `notifications` queue.
2. Channel adapters: SMTP email (HTML + text), Africa's Talking SMS, WhatsApp Cloud API,
   and in-app.
3. Failures retry with exponential backoff, from the 10-minute retry job.
4. Once a message is sent, its context is wiped.
5. The `dedupe_key` (unique) stops a message being sent twice. Each template has `.txt`,
   `.html` and `.sms.txt` variants.

## 9. Billing (`billing/`)

| Plan | Monthly (GHS) | Active elections | Voters / election | Staff |
|---|---|---|---|---|
| Free | 0 | 3 | 1,000 | 3 |
| Professional | 500 | 10 | 20,000 | 15 |
| Enterprise | 2,500 | 100 | 250,000 | 200 |
| High Assurance | 7,500 | unlimited | unlimited | unlimited |

- **Plans:** seeded and synced after `migrate`. Limits are enforced when creating elections,
  importing voters and adding staff. Features (SSO, LDAP, SMS, trustees, API…) are gated
  per plan, with per-organization overrides.
- **Invoices:** subtotal (plan + per-election + voters above the included amount) − coupon,
  then:
  - levy = 6% of the discounted subtotal (`BILLING_LEVY_RATE`);
  - VAT = 15% of (subtotal + levy) (`BILLING_VAT_RATE`).

  Invoices are paid through Paystack (`Payment.purpose = INVOICE`).

## 10. Internationalization, accessibility, time

- `LocaleMiddleware` picks the language from the `django_language` cookie, then the
  `Accept-Language` header. English and French are available; voter-facing strings are
  translated in `locale/fr`.
- Times are stored in UTC. Each election has its own `timezone`, and pages render in the
  election's zone unless the voter picks another (`/prefs/timezone/`).
- Accessibility preferences (high contrast, large text, low bandwidth) are stored in a
  cookie and applied as `<html>` classes. Low-bandwidth mode drops web fonts, icons and
  images.

## 11. Observability

- **Metrics.** Prometheus multiprocess mode. Metrics include HTTP and database latency,
  vote submissions by outcome, vote latency, payments by status, webhooks, reconciliation
  discrepancies, fraud alerts, logins, rate-limit hits, notifications and queue depth (full
  list in [OPERATIONS.md](OPERATIONS.md#monitoring)). `/metrics` requires either
  `Authorization: Bearer $METRICS_TOKEN` or a signed-in platform admin.
- **Health.**
  - `/healthz/live`: process up.
  - `/healthz/ready`: database, cache and broker reachable, and migrations applied.
- **Logs.** JSON (`LOG_FORMAT=json`) with `request_id`, user and path. Secrets and PII are
  never logged.
- **Tracing and errors.** Sentry (`SENTRY_DSN`) and OpenTelemetry (`OTEL_EXPORTER_OTLP_ENDPOINT`).
- **Alerting.** Prometheus rules live in `deploy/monitoring/alert_rules.yml`:
  `HighServerErrorRate`, `PaymentSuccessRateLow`, `VoteSubmissionFailures`,
  `VoteLatencyHigh`, `DatabaseSlow`, `QueueBacklog`, `WebhookFailures`, `FraudAlertSpike`,
  `RateLimitingSpike` and `AppDown`. An audit-chain break is reported differently: it is
  logged at CRITICAL, written to the audit log and sent to every platform admin.

## 12. Known limitations

- **No coercion resistance:** a voter can be watched while voting, and re-voting is not
  supported.
- **Tally decryption is not verifiable:** there are no zero-knowledge decryption proofs.
  Integrity of the tally rests on signed results, recounts and trustee custody.
- **Insertion-order linkability:** a database superuser could compare ballot insertion order
  with `voted_at` (§3.1).
- **`SECRET_KEY` rotation invalidates live credentials:** outstanding access codes, OTPs,
  ballot sessions and recovery codes stop working. Rotate it between elections and reissue
  credentials afterwards.
- **USSD needs Paystack mobile-money charge support** for the payer's network.
