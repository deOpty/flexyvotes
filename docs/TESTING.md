# Testing Guide

## 1. Running the tests

```bash
# Fast: SQLite, eager Celery, no external services
pip install -r requirements-dev.txt
python manage.py test

# Full: PostgreSQL + Redis in Docker (includes the race-condition and trigger tests)
docker compose run --rm web python manage.py test --noinput --parallel 1

# One module, class or test
python manage.py test elections.tests.test_tally
python manage.py test payments.tests.test_payments.PaymentServiceTests
python manage.py test core.tests.test_i18n.FrenchLocaleTests.test_ballot_flow_in_french
```

The test runner (`core.testing.FlexyTestRunner`, set as `TEST_RUNNER`) clears the cache
before every test, so rate-limit counters and cached permissions never leak between tests.

When the command is `manage.py test`, settings switch to a safe test profile:
- MD5 password hashing, for speed;
- the locmem email backend;
- eager Celery with the `memory://` broker;
- every integration disabled: Paystack, Africa's Talking, WhatsApp, CAPTCHA, KMS, Sentry
  and SSO client ids.

So a developer's real `.env` can never make tests call live services. Outbound HTTP is
mocked with `responses`.

| Database | Result |
|---|---|
| SQLite | All tests run, except 5 concurrency tests and 1 trigger test, which skip because they need PostgreSQL |
| PostgreSQL | All tests run, except 1 SQLite-only tamper test. It edits a ballot row directly, which the append-only trigger blocks on PostgreSQL; the trigger tests assert that block instead. |

## 2. What is covered

**203 tests** in 16 modules.

| Module | Tests | Covers |
|---|---|---|
| `core/tests/test_crypto_audit.py` | 18 | Envelope encryption and AAD binding; data-key rotation; KEK rotation; upgrade from the derived KEK to a configured KEK and to KMS; `verify_integrity` fails when keys can't decrypt; encrypted columns stored as ciphertext; Ed25519 signatures; ballot sealing; Shamir thresholds; Merkle proofs; passphrase backups; audit chain linking, ORM refusal, tamper and delete detection, PostgreSQL triggers |
| `core/tests/test_rbac_security.py` | 24 | Organization-admin tenant scoping; event-scoped roles; read-only auditor; platform admin; role sync on migrate; CSP nonce and headers; correlation id; no-cache on sensitive pages; health endpoints, including load-balancer `Host` headers; metrics token; `security.txt` (contact, RFC 9116 expiry); API CORS; POST-only logout; open-redirect guard; SSRF allow-list and public-IP resolution; circuit breaker; rate-limit window; idempotency records; trusted-proxy client IP; CSV formula injection; spoofed uploads; OTP brute force |
| `core/tests/test_auth_sso.py` | 11 | Lockout; TOTP enrolment, second step and replay protection; new-device alert and email step-up; session listing and remote revocation; enforced staff MFA; API token lifecycle; weak passwords and bots rejected at registration; staff SSO login; OIDC `nonce` / audience / `state` checks; voter SSO matched to the roll by verified email |
| `core/tests/test_i18n.py` | 4 | French catalog compiled; public pages and the ballot flow render in French; English remains the default |
| `elections/tests/test_ballot.py` | 9 | Normalizing every ballot type; too many selections; duplicate or foreign candidates; abstention rules; score range; positions outside the voter's ballot style; withdrawn candidates; form parsing and rank gaps; configuration problems |
| `elections/tests/test_tally.py` | 12 | Plurality with percentages; seat ties reported, not broken; multi-seat block voting; unknown ids ignored; IRV first-round majority, transfers and exhausted ballots; STV with surplus transfer; reproducible lot; score totals; referendum threshold; dispatch by type |
| `elections/tests/test_secret_ballot.py` | 12 | Opening creates key, snapshot and freezes; ballots encrypted and unlinkable; identical choices give unrelated ciphertexts; double voting and token reuse; re-issuing revokes the old token; invalid ballots rejected atomically; paused elections and suspended voters; full tally → certify → publish → verify; tampered ballot counted invalid; separation of duties; transition permissions; automatic close |
| `elections/tests/test_web_flows.py` | 15 | Full voter journey (sign in → ballot → review → explicit confirm → receipt); generic, audited error on a wrong code; rate limiting per IP and per identifier; code-only voters don't share a limit (regression); student-ID mode; required second factor; email OTP without account enumeration; lost codes sent only to the roll email; self-registration with email verification; closed election refuses sessions; console pages through the lifecycle; other tenants locked out; public pages; transition endpoint and separation of duties |
| `elections/tests/test_integrity_voters.py` | 19 | Dual approval (different approver, mandatory reason, rejection and expiry); unfreeze while open; edit-policy matrix; legal hold; disputes, incidents and evidence; reopen after close; k-of-n trustees needed to tally; constituency tree; CSV (incl. legacy headerless) and XLSX import; eligibility rules and ballot styles; credential reset, export and suspension; plan voter limit; voter list frozen after open; anonymous code generation; small constituencies suppressed |
| `elections/tests/test_concurrency.py` | 5 | **PostgreSQL only.** 10 threads submitting one token store exactly one ballot; racing sign-in and cast; concurrent audit appends keep the chain intact; concurrent webhook replays credit once; concurrent identical idempotency keys create one payment |
| `payments/tests/test_payments.py` | 26 | Quotes (bundles, discounts, campaign price); per-voter limits; exactly-once crediting; amount mismatch held; idempotency key; failed and abandoned payments; receipts (bearer link, no payer details); callback verification; webhook signature and replay; refunds below and above the dual-approval threshold; chargebacks; reconciliation recovers a missed webhook; card vote-limit hold; disposable email and bot user agent; quote endpoint; full simulator flow; USSD token and mobile-money charge; ticket purchase amount check; tie-breaker vote; Ghana MoMo provider detection; finance console permissions; reconciliation scoped per tenant (finance officer, auditor, platform admin) |
| `fraud/tests/test_engine.py` | 9 | Clean requests allowed and not recorded; thresholds; IP velocity; blocklists and anonymizers; proxy flagging toggle; unexpected country; fraud console; blocklist entries scoped per tenant (effect and visibility) |
| `notifications/tests/test_notifications.py` | 6 | Queued then delivered with the context wiped; dedupe key; SMS skipped when not configured; SMS via Africa's Talking; WhatsApp; in-app |
| `billing/tests/test_billing.py` | 6 | Plan seeding and default Free subscription; feature-flag override; limits; invoice with usage, coupon, levy and VAT; trial and renewal; billing pages |
| `api/tests/test_api.py` | 10 | OpenAPI schema and docs; authentication and error envelope; password → token exchange; tenant scoping; election, position, candidate and voter creation plus transition; validation-error envelope; ballot session and idempotent vote; results hidden until published; audit endpoints; payment idempotency |
| `voting/tests.py` | 17 | Home lists only public elections; live counts on paid events, hidden for secret ballots; contact form creates a ticket; organizer approval and personal organization; negative prices rejected; organizer tools respect RBAC and lifecycle; edit-event keeps price and converts timezone; scanner checks in once; formula-safe guest list; ticket lookup; CSRF on ticket email; `seed_admin`; legacy vote URLs forward; legacy code URLs need permission; officer role scoping |

## 3. Testing patterns

- **`on_commit` work** (emails, notifications, confirmations): wrap the action in
  `with self.captureOnCommitCallbacks(execute=True):`. Plain `TestCase` never commits, so
  without this the callbacks never run.
- **Concurrency:** use `TransactionTestCase` with real threads, each closing its own DB
  connection, and skip unless `connection.vendor == 'postgresql'`. See
  `elections/tests/test_concurrency.py`.
- **External HTTP:** use `@responses.activate` and register Paystack and OIDC endpoints.
  An unregistered URL raises, so nothing reaches the network.
- **Fixtures:** `core/tests/factories.py` provides `make_user`, `make_org`, `grant`,
  `make_event`, `add_position`, `add_voters`, `open_institutional`, and
  `institutional_setup(dual=False, prefix='')`. The last one builds an organization, an
  admin, a reviewer, a two-position election and three voters with access codes. Use
  `prefix` when a test needs two independent setups.
- **Settings:** use `override_settings(...)`. After changing anything key-related, call
  `core.crypto.reset_key_caches()`.

## 4. CI pipeline (`.github/workflows/ci.yml`)

| Job | Steps |
|---|---|
| `test` | PostgreSQL 16 + Redis 7 services → install dev requirements → `compilemessages` → `makemigrations --check` → `check --deploy` (error level) → full test suite |
| `security` | Bandit on all apps (`pyproject.toml`), then `pip-audit --strict` |
| `container` | Build the image → Trivy (fails on HIGH or CRITICAL) → start the stack → OWASP ZAP baseline (`.zap/rules.tsv`) |

## 5. Security scanning locally

```bash
bandit -c pyproject.toml -r core elections payments fraud notifications billing api voting vote_fund
pip-audit -r requirements.txt --strict
docker build -t flexyvotes:scan . && trivy image --severity HIGH,CRITICAL flexyvotes:scan
```

## 6. Manual end-to-end check

Run it before a release, against `docker compose up` with `DEBUG=True` and no Paystack
key, so the payment simulator is on.

1. **Organization and team.** Sign in as the seeded admin. Create an organization in
   `/console/organizations/` and add a second user as **Election Reviewer**.
2. **Election setup.** Create an institutional election. Add positions of different
   ballot types. Import voters from CSV. Check the ballot preview.
3. **Approval.** Submit for review. Confirm the submitter can't approve. Approve as the
   reviewer. Schedule, then open.
4. **Voting.** In a private window, vote at `/e/<id>/vote/` with a voter's access code.
   Check that the receipt tracker is found at `/verify/<id>/`. Then try to vote again: you
   should see "already voted".
5. **Results.** Close voting, start the tally, certify as a second user, publish. Download
   `bundle.json` and run `python tools/verify_election.py bundle.json`.
6. **Paid voting.** Create a paid event and buy votes through the simulator. Check that the
   receipt shows the credited votes, and the console shows the payment and its risk score.
7. **Language.** Switch to French with the language selector, and check the voter pages.
8. **Staff security.** Turn on TOTP at `/account/security/`, then sign out and back in.
