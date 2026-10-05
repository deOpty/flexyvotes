# Implementation Status

This page maps every section of [FEATURES.md](FEATURES.md) to where it is implemented and
how it is tested.

**Status key**
- **Done**: implemented and covered by automated tests.
- **Partial**: the core is implemented; the listed parts are not.
- **Infra**: provided by the AWS deployment, documented in [DEPLOYMENT.md](DEPLOYMENT.md),
  rather than by application code.

## Summary

| # | Area | Status |
|---|---|---|
| 1 | Core voting engine | Done |
| 2 | Paid voting with Paystack | Done |
| 3 | Institutional elections (voters, constituencies) | Done |
| 4 | Strong voter authentication | Done |
| 5 | Ballot secrecy | Done |
| 6 | Cryptographic security | Partial: no zero-knowledge tally proofs |
| 7 | Anti-fraud and anti-abuse | Done |
| 8 | Election integrity controls | Done |
| 9 | Role-based access control | Done |
| 10 | Audit logging | Done |
| 11 | Results and tallying | Done |
| 12 | Real-time infrastructure | Done / Infra; polling instead of WebSockets |
| 13 | Database design | Done; private documents on a file volume (EFS), not S3 |
| 14 | API architecture | Done |
| 15 | Idempotency | Done |
| 16 | Paystack reconciliation | Done |
| 17 | Notifications | Partial: no mobile / browser push (in-app inbox instead) |
| 18 | Admin dashboard | Done |
| 19 | Candidate portal | Done |
| 20 | Voter experience | Done |
| 21 | Accessibility | Done; no formal WCAG audit yet |
| 22 | Internationalization | Done (English, French) |
| 23 | Observability | Done |
| 24 | Disaster recovery | Done; restore test logged |
| 25 | Security engineering | Done; no external penetration test yet |
| 26 | Testing | Done; Redis and Celery checked in the stack smoke test, not the unit suite |
| 27 | Race-condition protection | Done |
| 28 | Election lifecycle | Done |
| 29 | Multi-tenancy | Done |
| 30 | Billing and SaaS | Done |
| 31 | Public verification | Done |
| 32 | Independent auditor access | Done |

## Detail

### 1. Core voting engine: Done

- **Election types:** single choice, multiple choice, ranked (IRV/STV), referendum,
  approval, FPTP and score. `Category.BallotType`; `elections/ballot.py`, `elections/tally.py`.
- **Positions and categories:** many per election (`voting.Category`).
- **Candidates:** profiles, photos, manifestos, documents (`CandidateDocument`, private
  storage).
- **Ballot preview** at `/console/elections/<id>/ballot/preview/`.
- **Schedule:** start and end dates, election timezone, rendering in the voter's timezone.
- **States:** the full lifecycle, with automatic open and close (`lifecycle_tick`, every
  60 s).
- **Limits:** per election (`max_votes_per_voter`, per transaction, spend) and per position
  or candidate (`Category.max_votes_per_voter`, `Candidate.max_votes_per_voter`), plus
  `min_select` / `max_select`.
- **Eligibility rules:** `EligibilityRule`, by attribute or constituency, per election or
  per position.
- **Validation:** done on the server for every cast.
- **Duplicates and idempotency:** single-use authorization plus partial unique constraint;
  `Idempotency-Key` on the API.
- **Transactions:** one transaction with row locks.
- **Tests:** `test_ballot`, `test_tally`, `test_secret_ballot`, `test_integrity_voters`,
  `test_concurrency`.

### 2. Paid voting with Paystack: Done

- **Channels:** card, bank, mobile money, USSD, all through Paystack.
- **Payment flow:** initialize, verify, webhooks with HMAC-SHA512, reconciliation.
- **Idempotent crediting:** `apply_gateway_result` and the `VoteTransaction.payment`
  OneToOne.
- **Failed and abandoned payments:** handled by the state machine and `expire_abandoned`.
- **Tracking:** payment references, payment → vote mapping, append-only `PaymentEvent`
  audit trail.
- **Pricing:** price per vote, per event or per position (campaign pricing); bundles with
  bonus votes and promotions; discount codes.
- **Limits:** spend caps, and vote caps per payer or card (`card_vote_limit` hold).
- **Money operations:** refunds (dual approval above the threshold), chargebacks,
  reconciliation dashboard, revenue report with CSV export.
- **The architectural rule is met:** votes are credited only after server-side verification
  or a signed webhook, never on a client "success" response.
- **Tests:** `payments/tests/test_payments.py` (26 tests), plus the concurrency tests.

### 3. Institutional elections: Done

- **Import:** CSV and Excel (`elections/voters.py`), including the legacy header-less
  format.
- **Identity:** student, staff or member ID (`Voter.identifier`).
- **Verification:** email verification (OTP), phone verification (SMS OTP), SSO (Google and
  Microsoft OIDC, per-organization OIDC), LDAP / Active Directory.
- **Statuses:** Eligible, Verified, Voted, Suspended, Ineligible.
- **Constituency tree:** materialized path, any depth. Positions are limited to a
  constituency, and eligibility comes from the tree.
- **Tests:** `test_integrity_voters` (import, constituencies, eligibility), `test_web_flows`,
  `test_auth_sso`.

### 4. Strong voter authentication: Done

- **Methods per election:** code, email OTP, SMS OTP, SSO, LDAP, or a platform account
  (email + password).
- **Second factor:** can be required per election.
- **Staff security:** TOTP 2FA with recovery codes, passkeys (WebAuthn), session list and
  revocation, known devices.
- **Suspicious sign-ins:** a new device triggers an alert and an email step-up.
- **Abuse protection:** rate limits per IP and per identifier, lockout, honeypot and
  CAPTCHA (Turnstile, hCaptcha or reCAPTCHA).
- **Tests:** `test_auth_sso`, `test_web_flows`, `test_rbac_security`.

### 5. Ballot secrecy: Done

- **Separation:** Voter → VoteAuthorization (token hash only) → anonymous Ballot, with no
  FK, no timestamp and no token. Choices are encrypted to the election key.
- **Audit:** the log records participation only.
- **Tests:** `test_ballot_is_encrypted_and_unlinkable`,
  `test_identical_choices_produce_unrelated_ciphertexts`.
- **Residual risk:** insertion-order timing for a DB superuser, documented in
  [TRD §3.1](TRD.md#31-unlinkability).

### 6. Cryptographic security: Partial

**Done**
- TLS (ALB or nginx, HSTS).
- Encryption at rest: AES-GCM envelope encryption plus RDS encryption, with a KMS-held KEK.
- Encrypted ballots (X25519 ECIES).
- `secrets`-module randomness.
- Signed configuration snapshots and results (Ed25519).
- Hash-chained audit log.
- Receipts (trackers) that don't reveal the choices.
- Merkle inclusion proofs.
- Rotation (`keys rotate`, `rewrap`) and key backup (`keys backup`).
- Trustee k-of-n custody, so keys can be kept away from the app servers.

**Not done**
- Zero-knowledge proofs of correct decryption, and mix-nets (full E2E-V). Integrity of the
  tally instead rests on signed results, recounts and trustee custody
  ([TRD §12](TRD.md#12-known-limitations)).

### 7. Anti-fraud and anti-abuse: Done

- **Risk score 0–100** with the four bands from the spec; configurable thresholds.
- **Signals:** disposable email, automation user agent, IP / device / email velocity,
  repeated failed payments, shared device / IP / card across payer emails (account farms),
  Tor / VPN / proxy ranges, chargebacks, unexpected country, high amount, candidate bursts.
- **Scans:** vote bursts, coordinated campaigns, unusual geography.
- **Holds:** high scores are held for an analyst, never silently dropped.
- **Blocklist:** platform-wide or per organization.
- **Tests:** `fraud/tests/test_engine.py`, plus the fraud cases in `test_payments`.

### 8. Election integrity controls: Done

- **Freezes:** configuration, ballot, candidates and voter list. Unfreezing needs dual
  approval.
- **Change history:** signed configuration snapshots plus audit diffs.
- **Dual approval:** unfreeze, extend, reopen, bulk credential reset, decertify, release
  legal hold, large refunds.
- **Roles and separation of duties:** election officer, reviewer and results officer roles.
  The submitter can't approve, and the tallier can't certify.
- **Workflows:** result approval, recount, disputes, incidents, evidence (SHA-256,
  append-only), legal hold.
- **Tests:** `test_integrity_voters`, `test_secret_ballot`.

### 9. Role-based access control: Done

- The 11 roles in the spec, plus Election Reviewer. 43 permissions, assigned through roles
  and checked with `has_perm`, never by role name.
- Scoped to the platform, an organization or one election. Matrix in
  [SECURITY.md §3](SECURITY.md#3-access-control).
- **Tests:** `test_rbac_security`.

### 10. Audit logging: Done

- **Each event records** actor, action, time, IP, user agent, target, before/after diff,
  correlation id, result and reason.
- **Tamper-evident:** a hash chain per organization, append-only in both the ORM and
  PostgreSQL triggers, verified every 6 hours and by `verify_integrity`.
- **Tests:** `test_crypto_audit`.

### 11. Results and tallying: Done

- **Visibility:** live counts, or hidden until close or until publish.
- **Figures:** ranking, percentages, turnout, votes cast, invalid ballots, abstentions;
  constituency-level results (with an anonymity threshold) and candidate-level results;
  historical results (`/results/`).
- **Workflow:** automatic tally, manual and independent recount (Auditor role),
  certification, publication.
- **Exports:** CSV, Excel and PDF, plus a JSON API.
- **Tests:** `test_tally`, `test_secret_ballot`, `test_api`.

### 12. Real-time infrastructure: Done / Infra

**In the application**
- Redis (cache, rate limits, broker), Celery workers and beat, PostgreSQL.
- Stateless horizontal scaling; PgBouncer or RDS Proxy pooling; read-replica router.
- Circuit breakers (`core.http`), queue backpressure (503), rate limiting at nginx and in
  the app.
- A minimal vote path: one lock and one insert, with analytics deferred to Celery.

**Infra:** ALB, ECS autoscaling, CloudFront CDN.

**Partial:** live counts and monitoring poll JSON endpoints every few seconds. There are no
WebSockets.

### 13. Database design: Done

- PostgreSQL, Redis, Celery, Secrets Manager, and observability.
- **Separation:** Voter, VoteAuthorization, Ballot and Payment are deliberately separate
  tables ([DATABASE.md](DATABASE.md)).
- **Storage:** private documents use a filesystem volume (EFS in production); backups go to
  S3. An S3 storage backend for documents is not wired up.

### 14. API architecture: Done

- `/api/v1` with OpenAPI docs, Pydantic validation and response schemas.
- Pagination, idempotency keys, a consistent error envelope, correlation ids and rate limits.
- Every resource in the spec's example list exists ([API.md](API.md)).
- **Tests:** `test_api`.

### 15. Idempotency: Done

- `IdempotencyRecord`, the `Idempotency-Key` header, and `Payment.idempotency_key`.
- Webhooks are de-duplicated by payload hash. A repeated request returns the original
  result.
- **Tests:** `test_idempotency_key_returns_the_same_payment`, the concurrency tests,
  `test_payment_requires_idempotency_key_and_replays`.

### 16. Paystack reconciliation: Done

- Runs every 15 minutes: compares local records with the Paystack API, auto-resolves safe
  cases, and queues the rest for review.
- Covers missed, duplicate and delayed webhooks, as well as downtime.
- The dashboard is scoped per tenant.
- **Tests:** `test_reconciliation_recovers_missed_webhook`, `ReconciliationScopingTests`.

### 17. Notifications: Partial

- **Done:** email, SMS (Africa's Talking), WhatsApp and the in-app inbox.
- **Events:** invitation, registration, verification OTP, voting opened, reminder, vote
  confirmation, payment confirmation, election closing, results published, security alerts,
  approvals, organizer onboarding, support.
- **Delivery:** asynchronous through an outbox and Celery.
- **Not done:** mobile and browser push notifications.

### 18. Admin dashboard: Done

- `/console/` shows the dashboard figures from the spec: active elections, votes today,
  revenue, voters, fraud alerts.
- It also covers election, candidate, voter and payment management, fraud monitoring, the
  live election monitor, audit logs, results, reports, system health and support tickets.

### 19. Candidate portal: Done

- `/portal/`: invitation link, profile, photo, biography, manifesto documents.
- Shows campaign statistics, but only when results are public. Also shows election
  information and certified results.
- No voter data is exposed.

### 20. Voter experience: Done

- **Institutional:** sign in → candidates → choose → review → explicit confirm → cast →
  receipt.
- **Paid:** contestant → number of votes or bundle → Paystack → verified → credited →
  receipt.
- Mobile-first, and works without JavaScript.

### 21. Accessibility: Done (no formal audit yet)

- Labelled controls, keyboard navigation, visible focus, skip links, ARIA live regions,
  error summaries.
- High contrast, large text, low-bandwidth mode.
- WCAG 2.1 AA is the target. An independent accessibility audit is still recommended.

### 22. Internationalization: Done

- English and French (all voter-facing strings).
- Currency and timezone per election; localized dates and number formatting (`fv`
  template tags).
- Regional payment methods (Ghana MoMo providers, USSD).
- **Tests:** `test_i18n`.

### 23. Observability: Done

- JSON logs with correlation ids, Prometheus metrics (every metric listed in the spec),
  OpenTelemetry tracing, Sentry.
- Liveness and readiness probes, DB and queue metrics, payment monitoring, alert rules.

### 24. Disaster recovery: Done

- RPO and RTO defined; RDS PITR; cross-region snapshot copies; encrypted logical backups to
  S3 with Object Lock.
- Restore scripts plus an automated restore test. The first test was run and logged
  ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)), along with a DR-region procedure.

### 25. Security engineering: Done (no external penetration test yet)

- **Web protections:** OWASP Top 10 controls, CSRF, XSS (auto-escape + nonce CSP), ORM
  only, SSRF allow-list, secure cookies, HSTS, CSP, CORS allow-list, security headers.
- **Account protections:** rate limiting, lockout, step-up, secrets management.
- **Scanning in CI:** pip-audit, Bandit, ZAP baseline, Trivy.
- **Identity:** SSO and LDAP mean institutions don't need platform passwords.
- **Open:** a third-party penetration test before high-stakes use.

### 26. Testing: Done

- **Size:** 203 automated tests.
- **Unit:** validation, eligibility, state transitions, payment states, limits, tally, fraud
  rules.
- **Integration:** PostgreSQL in CI, Paystack mocked over HTTP, webhooks, authentication.
- **End-to-end:** register → verify → vote → confirm → tally → publish, and pay → webhook →
  verify → credit, both through the web flow and the API.
- **Security:** race conditions, replay, duplicates, webhook replay, sessions, privilege
  escalation, enumeration, automated voting.
- **Partial:** Redis and Celery workers were checked on the running Docker stack (worker
  ping, beat-dispatched tasks completing). The automated suite runs Celery eagerly with an
  in-process cache.

### 27. Race-condition protection: Done

- Transactions, row locks, atomic updates, unique and partial-unique constraints, and
  idempotency.
- Tested under real concurrency on PostgreSQL (`test_concurrency.py`): 10 simultaneous
  submissions of one token store exactly one ballot.

### 28. Election lifecycle: Done

- The explicit state machine from the spec, plus PAUSED.
- Each state allows only certain operations (`edit_policy`). After CERTIFIED, changes need
  a controlled correction: decertify through dual approval.

### 29. Multi-tenancy: Done

- Every resource is scoped to an organization; queries are filtered through
  `events_for_user` / `organizations_for_user`.
- Covers the console, API, reconciliation, the fraud blocklist and the audit log.
- **Tests:** `test_org_admin_scoped_to_own_tenant`, `test_other_tenant_cannot_see_console`,
  `test_elections_are_tenant_scoped`, `ReconciliationScopingTests`, `BlocklistTenancyTests`.

### 30. Billing and SaaS: Done

- Free, Professional, Enterprise and High Assurance plans.
- Per-election and per-voter pricing; subscriptions and trials; invoices with Ghana levy and
  VAT; coupons; usage tracking; payment history; feature flags with per-organization
  overrides.
- **Tests:** `billing/tests/test_billing.py`.

### 31. Public verification: Done

- `/verify/<id>/` shows eligible voters, votes cast, turnout, status, result hash and
  certification, with independent checks.
- Includes a tracker lookup with a Merkle proof, `bundle.json`, and the offline verifier
  `tools/verify_election.py`.

### 32. Independent auditor access: Done

- The **Election Auditor** role is read-only. It can see:
  - configuration and eligibility;
  - audit logs;
  - vote totals;
  - reconciliation reports (read-only);
  - fraud reports;
  - cryptographic proofs and certification.
- It can run recounts, which compare and never change the result.
- **Tests:** `test_auditor_is_read_only`, `test_auditor_can_inspect_but_not_resolve`.

## Recommended next steps

1. A third-party penetration test and an accessibility audit before the first high-stakes
   election.
2. Verifiable decryption (for example, ElGamal with Chaum-Pedersen proofs) for High
   Assurance customers.
3. A WebSocket or SSE channel for live counts at very high audience sizes.
4. Browser push notifications.
5. S3 storage for private documents, as an alternative to EFS.
