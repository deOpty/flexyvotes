# Testing Guide

FlexyVotes uses Django's built-in test framework (`django.test.TestCase`). There is
no pytest configuration in this repo — all commands below use `manage.py`.

## 1. Running the test suite locally

### Setup

```bash
# from the repo root
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # macOS/Linux

pip install -r requirements.txt
```

You'll need a `.env` (or exported environment variables) that at minimum satisfies
`vote_fund/settings.py` — e.g. `PAYSTACK_SECRET_KEY`, Cloudinary credentials, and DB
settings. The test suite runs against Django's ephemeral test database, so a real
Postgres/production database is not required, but `PAYSTACK_SECRET_KEY` must be set
because `PaystackWebhookTests` signs requests with it via HMAC-SHA512.

### Run everything

```bash
python manage.py test
```

### Run only the `voting` app

```bash
python manage.py test voting
```

### Run a single TestCase class

```bash
python manage.py test voting.tests.PaystackWebhookTests
```

### Run a single test method

```bash
python manage.py test voting.tests.CastVoteWithCodeTests.test_valid_code_casts_vote
```

Add `-v 2` to any of the above for more verbose per-test output, and `--keepdb` to
skip recreating the test database on repeated runs during local iteration.

## 2. Current coverage

`voting/tests.py` contains 8 tests across 5 `TestCase` classes, all passing as of
this writing:

- **`VotingCodeModelTests.test_default_code_is_unique_per_instance`** — creates two
  `VotingCode` rows for the same event without specifying `code` explicitly and
  asserts the generated codes differ. Regression test for a bug where the model's
  default code generator was evaluated once at class-definition time (so every
  auto-generated code was identical and collided with the unique constraint).

- **`CastVoteWithCodeTests.test_get_request_does_not_crash`** — issues a GET to
  `cast_vote_with_code` and asserts it returns a 302 redirect instead of raising.
  Regression test for an `UnboundLocalError`: `candidate` used to only be assigned
  inside the `POST` branch of the view.

- **`CastVoteWithCodeTests.test_valid_code_casts_vote`** — POSTs a valid, unused
  `VotingCode` to `cast_vote_with_code` and asserts a 302 redirect and that the code
  is marked `is_used=True` afterwards. Happy-path check for the vote-casting flow.

- **`PaystackWebhookTests.test_invalid_signature_rejected`** — POSTs a webhook body
  with a bogus `X-Paystack-Signature` header and asserts the view returns 400.

- **`PaystackWebhookTests.test_malformed_json_with_valid_signature_returns_400`** —
  computes a *correct* HMAC-SHA512 signature (using `settings.PAYSTACK_SECRET_KEY`)
  over a deliberately non-JSON body and asserts the view still returns 400 rather
  than raising on `json.loads`.

- **`BuyTicketTests.test_sold_out_ticket_is_rejected`** — creates a `Ticket` with
  `quantity_available=1`, records one existing successful `TicketPurchase` against
  it, then POSTs `buy_ticket` for one more unit and asserts the request is rejected
  (302 redirect, no new `TicketPurchase` row created). Regression test for a missing
  stock/availability check in `buy_ticket`.

- **`ProcessScanAuthorizationTests.test_non_organizer_cannot_check_in_tickets`** — logs
  in as a user who is neither the event's organizer nor staff, POSTs to
  `process_scan`, and asserts a 403. Regression test for `process_scan` previously
  having no access-control check at all.

- **`ProcessScanAuthorizationTests.test_organizer_can_access`** — logs in as the
  event's organizer and POSTs a scan payload referencing a nonexistent ticket
  reference, asserting a 404 (i.e. the organizer passes the authorization check and
  reaches the "ticket not found" branch). Happy-path/authorization check for the
  organizer.

### Known gaps (not yet covered)

- **USSD callback state machine** (`voting.views.ussd_callback`, `voting/views.py`
  around line 812). This is a multi-level menu driven by Africa's Talking POSTing
  `sessionId`/`serviceCode`/`phoneNumber`/`text`, covering both the voting flow
  (candidate lookup by nominee code, vote count, mock "payment" confirmation that
  directly creates a `VoteTransaction`) and the ticket-buying flow (event list →
  ticket list → quantity → confirmation). None of the menu levels, input validation
  (non-numeric input, out-of-range selection index), or the final transaction-
  creation branches have tests.
- **Rate limiting on `login_view` and `register_view`** (`voting/views.py` around
  lines 208 and 230). Both use `django.core.cache` counters keyed by IP
  (`login_attempts_<ip>`, `register_attempts_<ip>`) that lock out after 5 and 3
  attempts respectively within a 60-second window. No test exercises the lockout
  threshold, the cache-key reset on successful login, or the admin-notification
  email sent on registration.
- **`send_ticket_email`** (`voting/views.py` around line 1207). This endpoint has
  several validations added since the original implementation: a 10-requests/60s
  IP rate limit, JSON parsing, rejection of purchases without a matching
  `paystack_reference`, silent no-op for USSD-synthesized buyer emails
  (`...@ussd.vote`), an allow-list of image types (`png`/`jpeg`/`jpg`), and a 5MB
  cap on the decoded image size (`MAX_TICKET_EMAIL_IMAGE_BYTES`). None of these
  branches are tested.
- **External integrations** — Cloudinary (media storage), Paystack (payment
  initialization/verification beyond the webhook signature checks already tested),
  and Africa's Talking (USSD/SMS) are not mocked or tested anywhere. The `responses`
  package is already listed in `requirements.txt` specifically for mocking these
  outbound HTTP calls but is not yet imported/used in `voting/tests.py`.
- **Template/HTML rendering** — no test asserts on rendered template content,
  context variables passed to templates, or that specific templates are used
  (`assertTemplateUsed`), e.g. for `login.html`, `register.html`,
  `ticket_success.html`, `verify_ticket.html`.

## 3. Recommended testing strategy going forward

- Keep using `django.test.TestCase` (or `TransactionTestCase` only where a test
  specifically needs real transaction/commit behavior — e.g. testing `on_commit`
  hooks). `TestCase` wraps each test in a rolled-back transaction against the test
  database, which is why the existing suite can freely create `Event`/`Candidate`/
  `Ticket` rows per test without cleanup code.
- For any test that hits `buy_ticket`'s Paystack initialization call, the
  `paystack_webhook` verification call, or anything else that calls out to Paystack's
  API over HTTP, use the `responses` library (already in `requirements.txt`) to
  register the expected Paystack endpoint and return a canned JSON body instead of
  making a real network call:

  ```python
  import responses

  @responses.activate
  def test_buy_ticket_initializes_paystack_transaction(self):
      responses.add(
          responses.POST,
          "https://api.paystack.co/transaction/initialize",
          json={"status": True, "data": {"authorization_url": "https://checkout.paystack.com/xyz", "reference": "TK-TEST01"}},
          status=200,
      )
      # ... POST to the buy_ticket view and assert on the redirect / response
  ```

  `responses` raises if a registered call is never made or if an unregistered URL is
  hit, which also guards against a test accidentally making a live call to Paystack.
- For `ussd_callback` tests, no HTTP mocking is needed for the *view itself* (it's an
  endpoint Africa's Talking calls into, not one that calls out to Africa's Talking) —
  post directly to the `ussd_callback` URL with the same `sessionId`/`serviceCode`/
  `phoneNumber`/`text` fields Africa's Talking sends, and assert on the returned
  `CON .../END ...` text at each menu level. Where the code under test *does* call
  the `africastalking` SDK (e.g. sending an SMS receipt), mock at the SDK boundary
  with `unittest.mock.patch`, e.g. `patch('voting.views.africastalking.SMS.send')`
  (adjust the import path to wherever the SDK is initialized in `views.py`), so the
  test never performs a real sandbox call.
- Add coverage for the rate-limiting paths on `login_view`/`register_view` by
  looping the client past the attempt threshold within a test and asserting the
  lockout message/behavior appears, then use `django.test.utils.override_settings`
  or clear the cache between tests (`django.core.cache.cache.clear()`) so lockout
  state doesn't leak between test methods if a non-`TestCase`-backed cache backend
  (e.g. `LocMemCache`) is configured.
- Add coverage for `send_ticket_email`'s new validation branches: oversized payload,
  disallowed image extension, missing/unknown `paystack_reference`, and the
  `@ussd.vote` short-circuit — each is a cheap, fast unit test since none require
  network mocking.
- Wire up CI so every push runs, at minimum:

  ```bash
  python manage.py check --deploy
  python manage.py test
  ```

  `check --deploy` catches missing/incorrect production-safety settings (e.g.
  `DEBUG=True`, missing `SECURE_*` settings, weak `SECRET_KEY`) before they reach
  production, and should be run alongside the test suite on every push/PR, not just
  before deploys.

## 4. Manual smoke-testing locally

1. **Start the dev server**

   ```bash
   python manage.py migrate
   python manage.py runserver
   ```

2. **Hit the key pages** in a browser against `http://127.0.0.1:8000/`:
   - `/login/` and `/register/` — register a new account, confirm the "pending
     approval" admin email logic doesn't error even if no superuser has an email
     configured, then log in.
   - Home/event listing and an event detail page — confirm candidates and tickets
     render.
   - `/vote/<candidate_id>/` (`cast_vote_with_code`) — try both a GET (should just
     redirect, not crash) and a POST with a real `VotingCode` created via the admin
     or shell.
   - Buy-ticket flow (`buy_ticket`) for an event with a `Ticket` that has
     `quantity_available` set — confirm you're blocked once you exceed availability.
   - `verify_ticket_view` and the organizer's `event_scanner`/`process_scan` pages —
     confirm a non-organizer/non-staff account gets a 403 on `process_scan`, and the
     organizer can check a ticket in.

3. **Paystack test-mode keys**: set `PAYSTACK_SECRET_KEY` (and any public key used in
   templates/JS) to your Paystack **test-mode** secret/public key from the Paystack
   dashboard. Use one of Paystack's documented test card numbers to complete a
   checkout end-to-end, then use the Paystack dashboard's "Send test webhook"
   feature (or `ngrok`/a tunnel pointed at your local server plus a manually-signed
   `curl` request, matching what `PaystackWebhookTests` does) to exercise
   `paystack_webhook` with a real, correctly-signed payload.

4. **Africa's Talking USSD sandbox**: create a sandbox app in the Africa's Talking
   dashboard, configure its USSD callback URL to point at your local server's
   `/ussd/` endpoint (again via `ngrok` or similar tunnel since Africa's Talking
   needs to reach it over the public internet), and use the dashboard's USSD
   simulator to walk through both menu flows (`1` for voting, `2` for buying
   tickets) end-to-end, checking that invalid nominee codes, non-numeric vote
   counts, and out-of-range menu selections all produce a graceful `END ...`
   message rather than a 500.

5. **Cloudinary**: set real (or sandbox) `CLOUDINARY_CLOUD_NAME` /
   `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` values, upload a candidate/event
   image through the admin or an organizer-facing form, and confirm the resulting
   image URL is served from Cloudinary rather than local `media/`.
