# Security Review & Remediation Log

This document records the security assessment performed on FlexyVotes, every
vulnerability found, the fix applied, and what — if anything — still requires
action from the team (credential rotation, product decisions, etc.). Findings
are ordered by severity.

Severity is assessed on realistic exploitability and business impact for this
app (a payment-driven voting/ticketing platform), not a generic CVSS score.

---

## 1. CRITICAL — Payment bypass: free votes and free tickets

**Where:** `voting/views.py` — `vote_success()` and `ticket_success()`.

**The bug:** When a voter/buyer's browser is redirected back from PayStack,
these views looked up the transaction by the `reference` query parameter and,
if its status was `Pending`, immediately flipped it to `Success` — with no
verification that a payment had actually happened. The `reference` is not a
secret: it is generated and returned to the *same browser* the moment the
Pending transaction is created (`initiate_vote` / `buy_ticket`), before the
user is ever sent to PayStack.

**Impact:** Any user could call `initiate_vote` (or `buy_ticket`) to create a
Pending transaction, note the reference from the redirect, and then visit
`/vote/success/?reference=<ref>` (or `/ticket/success/?reference=<ref>`)
directly — skipping PayStack entirely — to have their vote counted or ticket
issued for free. This directly undermines the platform's core monetization
and could be used to manufacture unlimited free votes (undermining the
integrity of every paid vote count) or free event tickets.

**Fix:** Added `verify_paystack_transaction()` in `voting/services.py`, which
calls PayStack's authoritative `GET /transaction/verify/:reference` endpoint
server-to-server. `vote_success` and `ticket_success` now only mark a Pending
transaction `Success` if PayStack itself confirms the transaction status is
`success`. If verification fails, the user sees a "payment not yet confirmed"
message instead of a completed vote/ticket. The `paystack_webhook` endpoint
(HMAC-signature verified) remains the primary confirmation path; this fixes
the fallback path used when the browser redirect arrives before the webhook.

**Tests:** `VoteSuccessPaymentBypassTests`, `BuyTicketTests.test_unverified_ticket_payment_is_not_credited` in `voting/tests.py`.

**Status:** Fixed and covered by regression tests.

---

## 2. CRITICAL — Live secrets committed to git history

**Where:** `.env` (tracked in git since an early commit), and `vote_fund/settings.py` (hardcoded Cloudinary credentials in recent commits, per `git log`: "Hardcoded Cloudinary keys to test bypass", "Hardcoded Cloudinary config...").

**The bug:** `.env` — containing the PayStack secret key, Africa's Talking
API key, a Gmail app password, and Cloudinary API credentials — was tracked
in git with no `.gitignore`, and the same Cloudinary credentials were also
hardcoded directly into `settings.py` source in recent commits.

**Impact:** Anyone with read access to the repository (or its history, even
after later commits remove the values) has these live credentials. This is a
full compromise of the payment secret, SMS/USSD account, email account, and
media storage account.

**Fix applied in this pass:**
- Added `.gitignore` (excludes `.env`, `db.sqlite3`, `media/`, `staticfiles/`, caches, venvs).
- Added `.env.example` as the template for required variables (no real values).
- Removed the hardcoded Cloudinary credentials from `settings.py`; it now reads `CLOUDINARY_STORAGE` entirely from environment variables.
- Ran `git rm --cached` on `.env`, `db.sqlite3`, and `media/**` so they are no longer tracked going forward (files remain on disk locally).

**Action still required from the team (cannot be done automatically):**
1. **Rotate every credential that was ever in `.env`**: PayStack secret key, Africa's Talking API key, the Gmail app password, and the Cloudinary API secret. Untracking the file does **not** remove it from git history — anyone with a clone of the repository (or access to the remote) can still recover the old values from earlier commits.
2. Generate a fresh, unique `SECRET_KEY` per environment (see finding #3) and never reuse the one from local `.env` in production.
3. Decide whether to rewrite git history to purge the old `.env` blob (e.g. `git filter-repo`). This is a destructive, force-push operation that rewrites every commit hash and was **not** performed automatically — do this only after rotating credentials, and coordinate with anyone else with a clone of the repo.

**Status:** Code-level exposure fixed; **credential rotation is a mandatory manual follow-up** before this app should be considered safe to operate in production.

---

## 3. HIGH — Placeholder Django `SECRET_KEY`

**Where:** `.env` had `SECRET_KEY=your_django_secret_key_here`.

**Impact:** `python manage.py check --deploy` flags this as `security.W009`.
A weak/predictable `SECRET_KEY` undermines session signing, password reset
tokens, and CSRF token generation.

**Fix:** Generated a strong random key (`django.core.management.utils.get_random_secret_key()`) for local `.env`. **Production/staging must each get their own independently generated key** — never reuse the local development key.

**Status:** Fixed locally; deployment guide (`DEPLOYMENT.md`) documents generating a fresh key per environment.

---

## 4. HIGH — Broken access control on ticket check-in (`process_scan`)

**Where:** `voting/views.py` — `process_scan()`.

**The bug:** The view required a logged-in user (`@login_required`) but never
checked that the user was the organizer of — or staff for — the specific
event being scanned. Every other event-scoped view in the codebase
(`event_scanner`, `event_guestlist`, `edit_event`, etc.) has this check;
`process_scan` was missing it.

**Impact:** Any authenticated user (e.g. a regular voter who self-registered)
could check in/burn tickets for *any* event, not just their own — a direct
authorization bypass with real financial/operational impact (marking paid
tickets as used, blocking legitimate attendees at the door).

**Fix:** Added the same `request.user != event.organizer and not request.user.is_staff` check used elsewhere, returning `403` for unauthorized users.

**Tests:** `ProcessScanAuthorizationTests` in `voting/tests.py`.

**Status:** Fixed and covered by regression tests.

---

## 5. MEDIUM — CSRF protection disabled where it wasn't needed

**Where:** `voting/views.py` — `process_scan()` had `@csrf_exempt`.

**The bug:** `process_scan` is a session-authenticated, state-changing
endpoint (it marks tickets as checked in). It was decorated `@csrf_exempt`
even though the calling template (`templates/voting/scanner.html`) already
sends a valid `X-CSRFToken` header on every request — the exemption was pure
downside with no corresponding benefit.

**Impact:** An attacker-controlled page could have triggered ticket check-ins
via a logged-in organizer's browser (classic CSRF), since Django would not
have required a valid token.

**Fix:** Removed `@csrf_exempt`. The existing frontend code needed no changes since it was already sending the token correctly.

**Status:** Fixed.

---

## 6. MEDIUM — Abusable public endpoint for sending arbitrary email attachments

**Where:** `voting/views.py` — `send_ticket_email()`.

**The bug:** This endpoint is intentionally public/unauthenticated (a buyer
without a login needs to (re)send their own e-ticket) and `@csrf_exempt`, but
had no rate limiting, no validation of the "image" content type, and no size
cap. It decoded attacker-controlled base64 data and emailed it as an
attachment from the platform's own Gmail account to a real buyer email
address.

**Impact:** Someone who obtained (or brute-forced) a valid ticket reference
could repeatedly trigger emails with arbitrary attached content sent from the
platform's mail account — a spam/phishing/reputation-abuse vector, and an
unbounded resource-consumption risk (arbitrarily large attachments).

**Fix:** Added per-IP rate limiting (10 requests/minute), an allow-list for
image type (`png`/`jpeg`/`jpg` only), a 5MB size cap, and proper exception
handling for malformed input instead of letting it 500.

**Status:** Fixed.

---

## 7. MEDIUM — Non-constant-time webhook signature comparison

**Where:** `voting/views.py` — `paystack_webhook()`.

**The bug:** The computed HMAC-SHA512 signature was compared to the header
value with plain `==`, which is not constant-time and is theoretically
vulnerable to a timing side-channel that could help an attacker forge a
signature byte-by-byte.

**Fix:** Switched to `hmac.compare_digest()`. Also added a `try/except` around `json.loads(request.body)` so a malformed body returns `400` instead of an unhandled exception (potential info leak via a stack trace / 500 error).

**Status:** Fixed.

---

## 8. MEDIUM — No brute-force protection on login

**Where:** `voting/views.py` — `login_view()`.

**The bug:** `register_view` already had per-IP rate limiting (3
attempts/minute) but `login_view` had none at all, allowing unlimited
password-guessing attempts against any account, including organizer/admin
accounts.

**Fix:** Added the same cache-backed per-IP rate limiting pattern to
`login_view` (5 attempts/minute, reset on success).

**Note:** This uses Django's `LocMemCache`, which is per-process — see
finding #12 for why this needs to move to a shared cache (Redis/Memcached)
before running multiple app instances/workers in production.

**Status:** Fixed for a single-process deployment; needs a shared cache for multi-instance deployments (tracked separately, see #12).

---

## 9. MEDIUM — Weak passwords accepted on registration

**Where:** `voting/views.py` — `register_view()`.

**The bug:** New accounts were created via `User.objects.create_user()`
directly. Django's `AUTH_PASSWORD_VALIDATORS` (configured in `settings.py`)
are only enforced through Django's forms/admin — calling `create_user`
directly bypasses them entirely, so any password (including `"123"`) was
accepted.

**Impact:** Weak organizer-account passwords are a real risk given
organizers can create events, manage payouts-adjacent data, and view
guest/voter PII.

**Fix:** Added an explicit `validate_password()` call before user creation, surfacing validation errors back to the registration form.

**Tests:** `RegisterViewTests` in `voting/tests.py`.

**Status:** Fixed and covered by regression tests.

---

## 10. LOW-MEDIUM — Unhandled exceptions causing 500s on bad input

**Where:** `initiate_vote()` (non-numeric `amount`), `cast_vote_with_code()`
(GET request crashed with `UnboundLocalError` because `candidate` was only
defined inside the `POST` branch), `paystack_webhook()` (malformed JSON).

**Impact:** Primarily an availability/robustness issue — a malformed or
adversarial request could 500. A 500 response could also leak a stack trace
if `DEBUG` were ever mistakenly left on in production.

**Fix:** Added input validation / `try-except` blocks returning clean error
responses (redirect with a flash message, or `400`) instead of crashing.

**Status:** Fixed.

---

## 11. LOW — Ticket overselling (business-logic / integrity issue)

**Where:** `voting/views.py` — `buy_ticket()`.

**The bug:** No check against `Ticket.quantity_available` before accepting a
purchase, so a ticket type could be sold far beyond its configured stock.

**Fix:** Added a check that sums existing `Success` purchases for the ticket and rejects new purchases that would exceed `quantity_available`.

**Tests:** `BuyTicketTests.test_sold_out_ticket_is_rejected`.

**Status:** Fixed.

---

## 12. LOW — USSD multi-step flow used unordered querysets

**Where:** `voting/views.py` — `ussd_callback()`, the ticket-purchase branch (`first_input == "2"`).

**The bug:** Each step of the USSD session independently re-queries
`Event.objects.filter(...).distinct()` / `event.tickets.filter(...)` and
indexes into the result by position (`events[event_index]`), with no
`order_by()`. On SQLite this happens to be stable (insertion order), but on
Postgres — the production database — row order without `ORDER BY` is
**undefined** and can differ between two queries in the same session,
meaning a user could be charged for/allocated a different event or ticket
type than the one they selected earlier in the same USSD session.

**Impact:** A data-integrity bug with direct financial impact (buyer pays for
ticket A, gets charged as/receives ticket B) once running against Postgres in
production.

**Fix:** Added explicit `.order_by('id')` to every occurrence of these queries in the USSD flow.

**Status:** Fixed.

---

## 13. INFORMATIONAL — Production hardening gaps (now addressed)

Prior to this review, `settings.py` had:
- No `MEDIA_URL`/`MEDIA_ROOT` defined at all — `vote_fund/urls.py` referenced
  `settings.MEDIA_URL`/`MEDIA_ROOT` unconditionally when `DEBUG=True`, which
  crashed with `AttributeError` on any local/DEBUG run. **(Also filed as a
  functional bug — see the bug list below.)**
- `ALLOWED_HOSTS` and no `CSRF_TRUSTED_ORIGINS` at all, hardcoded to a single
  Render.com domain — would silently be wrong/insecure on a new AWS domain.
- No `LOGGING` configuration (relying on Django defaults, which mostly
  discard output under gunicorn).
- `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE` were hardcoded `True`
  unconditionally — this actually breaks local HTTP development entirely
  (cookies silently never set), rather than being a vulnerability, but it's
  the kind of over-broad setting that tends to get "temporarily" disabled
  in ways that don't get re-enabled.
- No `X_FRAME_OPTIONS`, `SECURE_CONTENT_TYPE_NOSNIFF`, HSTS, or SSL-redirect configuration.

**Fix:** `settings.py` now defines `MEDIA_URL`/`MEDIA_ROOT`; makes
`ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` configurable via environment
variables; ties cookie-secure flags to `DEBUG` so local dev still works over
HTTP; adds `X_FRAME_OPTIONS=DENY` and `SECURE_CONTENT_TYPE_NOSNIFF=True`
unconditionally; and adds **opt-in** (env-var-gated) `SECURE_SSL_REDIRECT`
and HSTS settings that should be turned on once TLS termination is
confirmed working in front of the app (see `DEPLOYMENT.md`) — they default
to off so a deploy without HTTPS configured yet doesn't get redirect-looped
or lock itself out. Added a console-based `LOGGING` config suitable for
container log collection (e.g. CloudWatch Logs).

**Status:** Fixed; `SECURE_SSL_REDIRECT`/HSTS enabling is an explicit step in the deployment checklist once HTTPS is live.

---

## 14. INFORMATIONAL / Accepted risk — USSD "payment" is not actually verified

**Where:** `voting/views.py` — `ussd_callback()`.

**Finding:** Both the USSD voting flow and the USSD ticket-purchase flow
create a `VoteTransaction`/`TicketPurchase` with `status='Success'`
immediately, with no real mobile-money charge ever collected or verified.
`voting/at_service.py` contains a `trigger_mobile_money_checkout()` helper
for Africa's Talking mobile money, but **it is never called from any view** —
the integration exists but isn't wired in.

**Impact:** Every vote or ticket "purchased" via USSD is effectively free
today. This is a product/business decision as much as a security one — flag
it to the team explicitly rather than silently "fixing" it, since wiring in
real mobile-money collection is a feature-level change (async
payment-confirmation callback, session/state handling across the
checkout-then-confirm gap, etc.), not a one-line patch.

**Status:** Not fixed — **flagged for a product/engineering decision**. Documented in `docs/TRD.md` and `docs/PRD.md` as a known limitation.

---

## 15. HIGH (data integrity) — Uploaded media was silently written to local disk instead of Cloudinary

**Where:** `vote_fund/settings.py`.

**The bug:** `settings.py` set only the legacy `DEFAULT_FILE_STORAGE` /
`STATICFILES_STORAGE` settings. `Django==6.0.7` (the version this project is
pinned to) does not derive `default_storage` from those legacy settings the
way earlier Django versions did — it resolves storage exclusively from the
`STORAGES` dict. With `STORAGES` undefined, `default_storage` silently fell
back to Django's built-in `FileSystemStorage`, confirmed live via
`default_storage.__class__` printing `FileSystemStorage` instead of
Cloudinary's storage class.

**Impact:** Every uploaded image (event flyers/backgrounds, candidate
photos, product/ticket images) was actually being written to the
container's local, ephemeral filesystem instead of Cloudinary — the opposite
of what `settings.py`'s own comments and the git history ("FORCE Cloudinary
Storage for all media files") describe as intentional. Consequences: (1)
every uploaded image was lost on the next container restart/redeploy/scale
event, since container filesystems aren't persistent by default; (2) images
rendered as broken `/media/...` links whenever `DEBUG=False` (no local
static-media serving); (3) discovered in production as a real user-facing
404 on an event flyer.

**Fix:** Added a `STORAGES` dict to `settings.py` pointing `default` at
`cloudinary_storage.storage.MediaCloudinaryStorage` and `staticfiles` at
Whitenoise's `CompressedManifestStaticFilesStorage`. The legacy
`DEFAULT_FILE_STORAGE`/`STATICFILES_STORAGE` settings were kept (not
removed) because `django-cloudinary-storage`'s own bundled `collectstatic`
override reads `settings.STATICFILES_STORAGE` directly and raises
`AttributeError` at build time if it's absent entirely — both forms must
stay defined and in agreement.

**Verification:** rebuilt the Docker image and confirmed, against the
running container: `default_storage.__class__` is now
`cloudinary_storage.storage.MediaCloudinaryStorage`; `staticfiles_storage.__class__`
is still Whitenoise's `CompressedManifestStaticFilesStorage` (no regression);
static assets and all pages still serve `200`; all 13 tests pass; and an
actual test image upload was confirmed to reach Cloudinary and return a real
`res.cloudinary.com` URL.

**Remaining action:** rows created while this bug was active (before the
fix was deployed) still have local paths stored in the database and will
not retroactively appear in Cloudinary — those images need to be re-uploaded
through the app/admin once the fix is live.

**Status:** Fixed and verified end-to-end.

---

## 16. Verified as already correct (no change needed)

- **CSV export injection**: `download_codes` and `download_guestlist` already
  sanitize every string field via `sanitize_csv_value()` before writing to
  CSV, correctly neutralizing formula-injection (`=`, `+`, `-`, `@` prefixes).
- **`DEBUG` default**: defaults to `False` unless explicitly set via env var — correct.
- **Password hashing**: uses Django's default `User` model / `create_user`, which hashes with Django's configured (PBKDF2) hasher — no custom/weak hashing was introduced.

---

## Summary table

| # | Finding | Severity | Status |
|---|---|---|---|
| 1 | Payment bypass via `vote_success`/`ticket_success` | Critical | Fixed |
| 2 | Live secrets committed to git history | Critical | Code fixed; **rotation required (manual)** |
| 3 | Placeholder `SECRET_KEY` | High | Fixed (locally; per-env action needed) |
| 4 | Missing authorization on `process_scan` | High | Fixed |
| 5 | Unneeded CSRF exemption on `process_scan` | Medium | Fixed |
| 6 | Abusable `send_ticket_email` endpoint | Medium | Fixed |
| 7 | Non-constant-time webhook signature check | Medium | Fixed |
| 8 | No login rate limiting | Medium | Fixed (single-process) |
| 9 | Weak passwords accepted on registration | Medium | Fixed |
| 10 | Unhandled exceptions (500s) on bad input | Low-Medium | Fixed |
| 11 | Ticket overselling | Low | Fixed |
| 12 | Unordered USSD queries (Postgres data-integrity risk) | Low | Fixed |
| 13 | Missing production hardening (MEDIA settings, headers, logging) | Informational | Fixed |
| 14 | USSD "payment" never actually verified | Informational | **Open — product decision needed** |
| 15 | Uploaded media silently written to local disk instead of Cloudinary (`STORAGES` vs legacy settings) | High (data integrity) | Fixed and verified end-to-end |
| 16 | CSV injection guard, DEBUG default, password hashing | — | Verified correct, no change |

## Outstanding action items for the team

1. **Rotate all credentials** that were ever in `.env` (PayStack, Africa's Talking, Gmail app password, Cloudinary) — see finding #2.
2. **Generate a unique `SECRET_KEY` per environment** — never reuse the development key in staging/production.
3. **Decide on and schedule git history rewrite** to purge the old `.env` blob, after credentials are rotated.
4. **Decide the product direction for USSD payments** (finding #14) — wire in real Africa's Talking mobile-money confirmation, or explicitly scope USSD as a free/demo channel.
5. Before scaling to multiple app instances/workers, replace `LocMemCache`-based rate limiting with a shared cache (Redis/Memcached) — see `docs/TRD.md`.
6. **Re-upload any images that were saved while finding #15 was active** — those rows still point at local paths that don't exist in Cloudinary.
