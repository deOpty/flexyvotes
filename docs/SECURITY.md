# Security

This document covers FlexyVotes's threat model, the access-control matrix, the security
controls, how security is tested, and the log of every vulnerability found so far and how
it was fixed.

To report a vulnerability, write to the address in `/.well-known/security.txt` (set by
`SECURITY_CONTACT`). Please don't test against live elections.

## 1. Assets

| Asset | Why it matters |
|---|---|
| Vote counts and ballots | The outcome of competitions and elections |
| Ballot secrecy | Voters must not be linkable to their choices |
| Money | Paystack funds, refunds, organizer payouts |
| Voter roll PII | Names, emails, phone numbers, student IDs |
| Keys | KEK, signing key, election private keys, trustee shares |
| Audit log | The evidence that settles disputes |

## 2. Threat model

| Adversary | Goal | Main mitigations | Residual risk |
|---|---|---|---|
| Fan or bot farm | Free or inflated paid votes | Server-side Paystack verification, exact amount and currency match, one credit per payment, fraud scoring and holds, rate limits, CAPTCHA and honeypot, per-voter caps | Many distinct real cards and phones can still buy votes. That is legitimate paid voting, but anomaly scans flag it. |
| Stolen-card fraudster | Votes now, chargeback later | Card and device velocity, account-farm signals, chargeback reversal, blocklist | Revenue is lost on the chargeback, but the votes are reversed |
| Institutional voter | Vote twice, or vote while ineligible | Eligibility at issue time, single-use authorization (row lock + partial unique), ballot validated on the server | — |
| Credential thief | Vote as someone else | Codes and OTPs only go to roll contacts; lockout and rate limits; optional second factor; "you already voted" page with a dispute path | A voter who shares their code can be impersonated |
| Election official | See or change how people voted | No voter–ballot link; ballots sealed and append-only; trustee custody (k of n); signed results; recounts; audit | A colluding DB superuser could compare ballot insertion order with `voted_at` (timing) |
| Organizer | Change rules mid-election, fake results | Lifecycle locks, freezes, re-approval after changes, separation of duties, signed config snapshots, signed certification, public verification | — |
| Platform insider / DBA | Edit audit or ballots | Hash chain verified every 6 h; DB triggers block UPDATE and DELETE; changes need DDL, which is logged | Can drop the triggers. Detection only. |
| Network attacker | Steal sessions, tamper with traffic | TLS + HSTS, `Secure` / `HttpOnly` / `SameSite` cookies, CSRF, strict CSP | — |
| Web attacker | XSS, SSRF, injection, open redirect | Auto-escaping, `json_script`, nonce CSP, URL allow-list + public-IP check, ORM only, `safe_next()` for redirects, upload magic-byte checks | `style-src 'unsafe-inline'` is still allowed (for Bootstrap attributes) |

## 3. Access control

Authorization is permission-based (`core/rbac.py`). A user's permissions are the union of
their role assignments.
- **Scope:** each assignment applies to the platform, one organization (including all its
  elections) or one election.
- **Organizers:** an event's `organizer` gets the Organizer column below for that event.
- **Platform admins:** Django staff and superusers have every permission.
- **Separation of duties:** this is enforced in code, not by roles. The approver must not be
  the submitter, and the certifier must not be the tallier. This holds even for an
  Organization Admin, who has both permissions.

SA Super Admin · OA Organization Admin · EA Election Admin · ER Election Reviewer ·
EO Election Officer · AU Election Auditor · CM Candidate Manager · FO Finance Officer ·
SU Support Agent · FA Fraud Analyst · RO Results Officer

| Permission | SA | OA | EA | ER | EO | AU | CM | FO | SU | FA | RO | Organizer |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `election.create` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |
| `election.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `election.edit` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |
| `election.submit` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |
| `election.review` | ✓ | ✓ |  | ✓ |  |  |  |  |  |  |  |  |
| `election.publish` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |
| `election.pause` | ✓ | ✓ | ✓ |  | ✓ |  |  |  |  |  |  | ✓ |
| `election.close` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |
| `election.archive` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |
| `election.freeze` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |
| `candidate.create` | ✓ | ✓ | ✓ |  |  |  | ✓ |  |  |  |  | ✓ |
| `candidate.edit` | ✓ | ✓ | ✓ |  |  |  | ✓ |  |  |  |  | ✓ |
| `voter.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  |  | ✓ |  |  | ✓ |
| `voter.import` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |
| `voter.edit` | ✓ | ✓ | ✓ |  | ✓ |  |  |  |  |  |  | ✓ |
| `voter.credentials` | ✓ | ✓ | ✓ |  | ✓ |  |  |  |  |  |  | ✓ |
| `vote.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |  | ✓ |  | ✓ | ✓ | ✓ |
| `vote.export` | ✓ | ✓ |  |  |  |  |  |  |  |  |  |  |
| `results.view` | ✓ | ✓ | ✓ |  |  | ✓ |  |  |  |  | ✓ | ✓ |
| `results.tally` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  | ✓ | ✓ |
| `results.approve` | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |  |
| `results.certify` | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |  |
| `results.publish` | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |  |
| `results.recount` | ✓ | ✓ |  |  |  | ✓ |  |  |  |  | ✓ |  |
| `payment.view` | ✓ | ✓ | ✓ |  |  | ✓ |  | ✓ | ✓ | ✓ |  | ✓ |
| `payment.reconcile` | ✓ | ✓ |  |  |  |  |  | ✓ |  |  |  |  |
| `refund.create` | ✓ | ✓ |  |  |  |  |  | ✓ |  |  |  |  |
| `refund.approve` | ✓ | ✓ |  |  |  |  |  | ✓ |  |  |  |  |
| `pricing.manage` | ✓ | ✓ | ✓ |  |  |  |  | ✓ |  |  |  | ✓ |
| `audit.view` | ✓ | ✓ | ✓ | ✓ |  | ✓ |  |  |  | ✓ |  | ✓ |
| `fraud.view` | ✓ | ✓ |  |  |  | ✓ |  |  |  | ✓ |  |  |
| `fraud.review` | ✓ | ✓ |  |  |  |  |  |  |  | ✓ |  |  |
| `dispute.view` | ✓ | ✓ | ✓ |  | ✓ | ✓ |  |  |  |  |  | ✓ |
| `dispute.manage` | ✓ | ✓ |  |  |  |  |  |  |  |  |  |  |
| `incident.manage` | ✓ | ✓ | ✓ |  | ✓ |  |  |  |  |  |  | ✓ |
| `approval.decide` | ✓ | ✓ |  | ✓ |  |  |  |  |  |  |  |  |
| `org.manage` | ✓ | ✓ |  |  |  |  |  |  |  |  |  |  |
| `org.billing` | ✓ | ✓ |  |  |  |  |  | ✓ |  |  |  |  |
| `support.view` | ✓ | ✓ |  |  | ✓ |  |  |  | ✓ |  |  |  |
| `support.manage` | ✓ | ✓ |  |  |  |  |  |  | ✓ |  |  |  |
| `ticket.manage` | ✓ | ✓ | ✓ |  |  |  |  |  |  |  |  | ✓ |
| `store.manage` | ✓ |  |  |  |  |  |  |  |  |  |  |  |
| `platform.admin` | ✓ |  |  |  |  |  |  |  |  |  |  |  |

The `VOTER` role is a marker with no permissions. Granting and revoking roles is audited
(`ROLE_GRANTED` / `ROLE_REVOKED`).

## 4. Controls

| Area | Control |
|---|---|
| **Passwords** | Argon2id; min length 10, common-password, numeric and similarity validators |
| **Login** | Lockout after `LOGIN_MAX_FAILURES` (default 5) for `LOGIN_LOCKOUT_SECONDS` (900 s); 10/min per IP; failures audited; same error for unknown user and bad password |
| **MFA** | TOTP with replay protection and recovery codes; passkeys (WebAuthn); email step-up for new devices; enforced for staff with `ENFORCE_STAFF_MFA=True` |
| **Sessions** | 8 h max age (`SESSION_COOKIE_AGE`); `Secure`, `HttpOnly`, `SameSite=Lax`; session key rotated at login; server-side list with revoke and sign-out-others; logout is POST only |
| **SSO / LDAP** | OIDC with PKCE, `state`, `nonce` and JWKS validation; LDAP over TLS; per-organization configs encrypted |
| **CSRF** | Django CSRF on every form and on session-authenticated API calls. Exempt only where the caller is authenticated another way: Paystack webhook (HMAC) and USSD (token / IP). |
| **Headers** | Nonce-based CSP with no inline handlers; `X-Frame-Options: DENY` and `frame-ancestors 'none'`; `nosniff`; `Referrer-Policy: strict-origin-when-cross-origin`; `Permissions-Policy`; COOP and CORP `same-origin`; HSTS when `SECURE_HSTS_SECONDS` > 0 |
| **Output encoding** | Django auto-escaping; data passed to JavaScript only through `json_script`; CSV and Excel exports neutralise formulas (`=`, `+`, `-`, `@`); PDF exports escape values |
| **Input validation** | django-ninja and Pydantic schemas for the API; server-side ballot validation; positive-amount checks on every price field; size caps on uploads and on request bodies (`DATA_UPLOAD_MAX_MEMORY_SIZE`) |
| **Uploads** | Images limited to 2 MB. Documents limited to 10 MB, with checks on extension **and** magic bytes. Evidence and manifestos go to private storage, are streamed only by permission-checked views, and get a SHA-256 on upload. |
| **Redirects** | `safe_next()` allows only same-host relative URLs |
| **SSRF** | `core.http.validate_url`: HTTPS only, host allow-list, resolved IP must be public; timeouts and circuit breakers on every outbound call |
| **Rate limiting** | Redis-backed and shared across replicas; covers login, voter sign-in (IP and identifier), OTP, registration, payments, disputes, tracker lookups and the API; returns `429` with `Retry-After` |
| **Bot protection** | Honeypot field + signed form timestamp (`FORM_MIN_FILL_SECONDS`) on pay, register, voter sign-in, voter registration, dispute and contact forms; optional Turnstile, hCaptcha or reCAPTCHA (`CAPTCHA_PROVIDER`) |
| **Payments** | HMAC-SHA512 webhook check with a constant-time compare; server-side verify on every callback; exact amount and currency match; one credit per payment (DB-enforced); fake gateway refused when `DEBUG=False` |
| **Ballot secrecy** | Identity and ballot separated (§2 of [TRD.md](TRD.md#3-ballot-secrecy-and-cryptography)); confirmation emails don't carry the tracker; ballot exports contain only ciphertext and trackers |
| **Encryption at rest** | AES-256-GCM envelope encryption of PII and secrets, KEK in KMS or env; blind indexes for lookups; RDS storage encryption on top |
| **Secrets** | Only from the environment (AWS Secrets Manager in production); never logged; `.env` is git-ignored; the image build uses a throw-away key, so no secret is baked in |
| **Audit** | Hash-chained, append-only (ORM + DB triggers), verified every 6 h; records actor, IP, user agent, correlation id and field-level diffs |
| **Database** | Least-privilege app role; append-only triggers; `statement_timeout`; TLS to RDS (`DATABASE_SSL_REQUIRE`) |
| **Containers** | Non-root user, slim base, no build tools at runtime, healthchecks; Trivy scan in CI |
| **Admin surface** | Django admin at a configurable path (`ADMIN_URL`); pgAdmin only behind the `admin` profile and must be firewalled (finding A16) |

## 5. Security testing

| Kind | Tool / location | When |
|---|---|---|
| Security regression tests | `core/tests/test_rbac_security.py`, `test_auth_sso.py`, `test_crypto_audit.py`, `elections/tests/test_secret_ballot.py`, `payments/tests/test_payments.py`, `api/tests/test_api.py` | Every CI run |
| Race-condition tests | `elections/tests/test_concurrency.py` (PostgreSQL): 10 concurrent casts of one token, racing sign-in and cast, concurrent audit appends, concurrent webhook replays, concurrent identical idempotency keys | Every CI run |
| SAST | Bandit (`pyproject.toml` config). Currently 0 findings; the `# nosec` markers are reviewed false positives (test-only placeholders). | Every CI run |
| Dependency audit | `pip-audit -r requirements.txt --strict`. Currently 0 known vulnerabilities. | Every CI run |
| Container scan | Trivy on the built image (HIGH and CRITICAL fail the build) | Every CI run |
| DAST | OWASP ZAP baseline against a running stack (`.zap/rules.tsv`) | Every CI run |
| Deployment checks | `manage.py check --deploy` plus the custom checks `flexyvotes.W001`–`W006` (missing keys, unauthenticated USSD, …) | Every CI run and every deploy |

Covered by tests:
- RBAC denial in the console, finance, fraud and organizer tools, and on lifecycle
  transitions;
- object-level checks, such as an officer of org A acting on org B;
- CSRF enforcement (tested on the ticket-email endpoint; Django enforces it on every form);
- payment receipts work only with the unguessable reference and disclose no payer details;
- open redirects;
- webhook signature and replay handling;
- amount tampering;
- double casting and token reuse;
- tampered ballots counted as invalid;
- audit tampering and deletion detected;
- MFA replay;
- lockout;
- SSO `state` and `nonce` mismatch;
- OTP brute force;
- file-type spoofing;
- CSV injection;
- SSRF;
- security headers and CSP nonces.

## 6. Remediation log

### Part A: first security review (original app)

| # | Finding | Severity | Status now |
|---|---|---|---|
| A1 | Payment bypass: `vote_success` and `ticket_success` marked transactions paid from the `reference` query parameter | Critical | **Fixed.** Every callback verifies server to server; the rebuild routes everything through `apply_gateway_result()`. |
| A2 | Live secrets committed to git (`.env`, hardcoded Cloudinary keys) | Critical | Code fixed (`.gitignore`, env-only config). **Credential rotation and history purge are still manual** (§7). |
| A3 | Placeholder `SECRET_KEY` | High | Fixed; settings refuse to start without a key when `DEBUG=False` |
| A4 | Missing authorization on ticket check-in (`process_scan`) | High | Fixed; now needs `ticket.manage` on the event |
| A5 | Needless CSRF exemption on `process_scan` | Medium | Fixed |
| A6 | `send_ticket_email` could mail arbitrary attachments | Medium | Fixed; only the buyer's own ticket, rate limited |
| A7 | Non-constant-time webhook signature compare | Medium | Fixed (`hmac.compare_digest`) |
| A8 | No login brute-force protection | Medium | Fixed. The old per-process limiter is replaced by a Redis-backed one plus account lockout. |
| A9 | Weak passwords accepted | Medium | Fixed (validators, Argon2) |
| A10 | Unhandled exceptions (500s) on bad input | Low-Medium | Fixed; custom 400 / 403 / 404 / 429 / 500 pages; API error envelope |
| A11 | Ticket overselling | Low | Fixed (row lock on purchase) |
| A12 | Unordered USSD querysets | Low | Fixed |
| A13 | Production hardening gaps (headers, logging, media) | Info | Fixed and extended (CSP nonces, JSON logs, Sentry) |
| A14 | USSD "payment" never verified | Info (open) | **Fixed in the rebuild.** USSD starts a Paystack mobile-money charge and credits nothing until Paystack confirms it. The callback is authenticated by token and/or IP. |
| A15 | Uploads silently written to local disk instead of Cloudinary | High (data integrity) | Fixed (`STORAGES`). Rebuild note: the Cloudinary package's `collectstatic` override is no longer used (finding B12). |
| A16 | pgAdmin adds DB-access attack surface | Info | Kept behind the `admin` compose profile; it must be firewalled (§7) |
| A17 | CSV injection, `DEBUG` default, password hashing | — | Verified correct |

### Part B: platform rebuild (this release)

| # | Finding | Severity | Fix | Test / evidence |
|---|---|---|---|---|
| B1 | **Ballots linkable to voters.** The legacy code-voting flow stored the code used next to the candidate it voted for. | Critical (secrecy) | Rebuilt as voter → single-use authorization → anonymous sealed ballot (TRD §3) | `test_secret_ballot.py` |
| B2 | Stored XSS on analytics. Chart data was rendered with `\|safe`. | High | `json_script`, no inline scripts | Template compile + CSP |
| B3 | XSS on ticket pages. Values were interpolated into inline JS strings. | High | Data attributes + external JS | Template review; the nonce CSP blocks injected inline script |
| B4 | 14 known CVEs in dependencies (Django, urllib3, sqlparse) | High | Upgraded to Django 6.0.8, urllib3 2.8.0, sqlparse 0.6.0 | `pip-audit` clean |
| B5 | Logout over GET (cross-site logout) | Low | Logout is POST only | `test_logout_requires_post` |
| B6 | `mark_safe` nonce helper and unescaped HTML in exports (Bandit) | Medium | Removed `mark_safe`; `html.escape` in exports; dead legacy modules deleted | Bandit clean |
| B7 | Edit-event form overwrote price and fee with hardcoded values | Medium (integrity) | Form uses stored values; validated server side | `test_edit_event_keeps_price_and_converts_timezone` |
| B8 | Shamir reconstruction accepted wrong share sets silently | High (tally integrity) | SHA-256 checksum inside the secret; a wrong combination is rejected | `test_shamir_threshold` |
| B9 | Refund flow used a stale payment row (double refund possible under a race) | Medium | Re-fetch with a row lock before deciding | Refund tests in `test_payments.py` |
| B10 | **Append-only trigger migration failed on PostgreSQL**: the `%` in `RAISE` was read as a query parameter, so the protection could not be installed | High | Run the DDL verbatim (`params=None`) | New `test_database_triggers_make_tables_append_only` (PostgreSQL) |
| B11 | **KEK upgrade locked out existing data.** Setting `FIELD_ENCRYPTION_KEYS` after starting without it made every existing data key unreadable, and `keys rewrap` couldn't migrate. | High (data loss) | The derived KEK stays available for unwrap only; data keys are unwrapped by the provider that wrapped them (local → KMS works); `keys status` warns about stale keys | `test_upgrade_from_derived_kek_to_configured_kek`, `test_switch_from_local_kek_to_kms` |
| B12 | The Docker image couldn't build: settings refused to load without `SECRET_KEY`, and Cloudinary's `collectstatic` crashed on Django 6 | Medium (availability) | Build-step-only placeholder key; Django's own `collectstatic` (app order) | Image builds in CI |
| B13 | `restore.sh` relied on GNU-only `sha256sum --ignore-missing`, failing on BusyBox and Alpine | Medium (recovery) | Portable checksum check; warning when no checksum file is present | Backup → restore → `verify_integrity` round trip ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)) |
| B14 | `security.txt` expiry hardcoded, contact was a no-reply address | Low | Rolling 180-day expiry; `SECURITY_CONTACT` setting | `test_well_known_endpoints` |
| B15 | **Election-day lockout.** In code-only elections the per-identifier sign-in limit was keyed on a blank identifier, so all voters shared one counter: after 10 sign-ins in 10 minutes, everyone got 429. Per-IP limits (15/min app, 30 req/min nginx) would also throttle a campus behind one NAT IP. | High (availability) | The per-identifier limit applies only when an identifier is given. Per-IP limits are configurable (`VOTER_*_PER_IP_*`) with campus-friendly defaults. nginx limits are raised, with a `trusted_nat.conf` exemption. | `test_code_only_voters_do_not_share_a_rate_limit`, `test_guessing_one_voters_code_is_limited_per_identifier` |
| B16 | **Health probes rejected in production.** With a real `ALLOWED_HOSTS`, the ALB check (Host = target IP) and the image `HEALTHCHECK` (Host = 127.0.0.1) got 400, so containers would be marked unhealthy and replaced in a loop. Also, `/healthz/ready` showed its detailed checks to `Authorization: Bearer None` when no `METRICS_TOKEN` was set. | High (availability) / Low | `HealthCheckMiddleware` answers probes before host validation; the token compare is constant-time and needs a configured token | `test_probes_work_with_load_balancer_host_headers` |
| B17 | `verify_integrity` didn't check that keys can decrypt data, so a restore with the wrong KEK passed verification | Medium (recovery) | It now unwraps every data key and decrypts a sample of every encrypted column | `test_verify_integrity_fails_when_keys_cannot_decrypt` |
| B18 | **Cross-tenant reconciliation data.** Any organization's Finance Officer saw every tenant's open reconciliation items (payment references, amounts), could resolve any item by id, and could start platform-wide runs. Auditors could not see the reports at all. | High (tenant isolation) | Items are filtered to the viewer's organizations. Resolving needs `payment.reconcile` on that payment's event. Runs and their totals are platform-admin only. Auditors get read-only access. | `ReconciliationScopingTests` |
| B19 | **Cross-tenant fraud blocklist.** Any organization's fraud analyst could see the platform-wide blocklist (IPs, devices, card signatures), add entries that blocked voters on *every* tenant's events, and deactivate other tenants' entries | High (tenant isolation / availability) | Entries now carry an `organization` (empty means platform-wide, for platform admins only). The engine applies platform entries plus the event's own organization's entries. Analysts see and manage only their organizations' entries. | `BlocklistTenancyTests` |
| B20 | **CI migration check failed everywhere but one laptop.** The evidence `FileField` passed a storage *instance*, so the developer's absolute Windows `PRIVATE_MEDIA_ROOT` was baked into `elections/0001`. Every other machine (CI, Docker) saw a phantom model change. | Medium (delivery) | Storage passed as a callable (`core.storage.get_private_storage`), and the migration updated to match (no DB change) | `makemigrations --check` clean on Windows, Linux CI paths and in the image |
| B21 | `MEDIA_STORAGE=cloudinary` without credentials (the `.env.example` default) made every image URL raise, so pages with images returned 500 | Medium (availability) | Falls back to local storage; `check --deploy` reports `flexyvotes.W007` | `DeployCheckTests` |
| B22 | CI DAST/container hardening: ZAP scanned only an HTTPS redirect; CDN assets had no Subresource Integrity; static files sent `Access-Control-Allow-Origin: *`; the base image carried a fixable HIGH CVE (libpcre2) | Medium | Scan with redirect off; SRI on every pinned CDN asset; `WHITENOISE_ALLOW_ALL_ORIGINS=False`; `apt-get upgrade` in the image; ZAP accepted risks documented in `.zap/rules.tsv` | ZAP baseline 0 warnings; Trivy 0 HIGH/CRITICAL |

## 7. Outstanding actions for the team

1. **Rotate every credential that was ever committed** (A2): Paystack, Africa's Talking,
   Gmail app password, Cloudinary. Then decide whether to purge history with
   `git filter-repo`. That is a coordinated force-push.
2. **Set dedicated keys in every non-development environment.** Use `FIELD_ENCRYPTION_KEYS`
   or `KMS_KEY_ID`, plus `SIGNING_PRIVATE_KEY` and `BLIND_INDEX_KEY`, then run
   `keys rewrap` and `keys reindex`. Follow the procedure in
   [OPERATIONS.md](OPERATIONS.md#key-management). Before replacing a derived signing key,
   publish its public key in `SIGNING_PREVIOUS_PUBLIC_KEYS`.
3. **Set `USSD_CALLBACK_TOKEN`** (and `USSD_ALLOWED_IPS` with Africa's Talking's ranges).
4. **Firewall pgAdmin** to admin IPs or a VPN, and give it a unique password (A16).
5. **Production flags:** turn on `ENFORCE_STAFF_MFA`, set `METRICS_TOKEN`, choose a
   `CAPTCHA_PROVIDER` for high-profile paid events, and set `SECURITY_CONTACT`.
6. **Paystack allow-list:** if the Paystack key has an IP allow-list, add the NAT gateway
   or EC2 egress IPs. Otherwise verification and reconciliation fail with "Your IP address
   is not allowed" (this was seen when running locally).
