# FlexyVotes — Endpoint Reference

This document describes every URL registered in `vote_fund/urls.py` and `voting/urls.py`, grounded in the actual
implementation in `voting/views.py`. FlexyVotes is primarily a server-rendered Django app: most endpoints accept
HTML form POSTs and respond with a redirect (following the `messages` framework for user feedback) or a rendered
template. A small number of endpoints are true JSON/API or machine-callable protocol endpoints — those are called
out explicitly.

Base URL used in examples: `https://flexyvotes.example.com` (replace with the real `SITE_URL`).

Unless otherwise noted:
- "Auth: none" means the view has no `@login_required` and does no manual authentication check.
- "Organizer/staff of event" means: `request.user == event.organizer or request.user.is_staff`.
- All HTML-rendering views accept `GET` unless stated; POST-only actions redirect back to a referring page for any
  non-POST request that isn't otherwise handled.
- CSRF: standard Django CSRF protection applies to all form-POST and template-rendered views (they rely on
  `{% csrf_token %}` in the templates). Only endpoints explicitly marked `@csrf_exempt` skip this.

---

## 1. Public Pages

### `GET /`  — Home
- **View**: `home`
- **Auth**: none
- **Params (GET)**: `q` (string, optional) — free-text filter on event title (`icontains`).
- **Behavior**: Lists active events (`is_active=True`), ordered by `-start_date`; pops a one-shot
  `show_registration_popup` flag out of the session to drive a UI popup.
- **Response**: HTML render of `voting/home.html` with `events`, `show_popup`.

### `GET /event/<int:event_id>/` — Event detail
- **View**: `event_detail`
- **Auth**: none (organizer/admin get an extra UI flag)
- **Params**: none
- **Behavior**: 404s if event doesn't exist. Computes `is_expired` (now > end_date). Annotates each candidate with
  `vote_count` (sum of `Main` vote-type successful transactions) and `tie_breaker_count` (sum of `Tie-Breaker`
  successful transactions), sorted by vote count desc, then tie-breaker desc, then name. Computes each candidate's
  `percentage` of total **main** votes. Sets `is_organizer_or_admin` if the logged-in user is staff or an approved
  organizer (used by the template to show management controls — note this is not scoped to *this* event's organizer).
- **Response**: HTML render of `voting/event_detail.html`.

### `GET /contact/` and `POST /contact/` — Contact page
- **View**: `contact_view`
- **Auth**: none
- **Params (POST)**: none read server-side (message body is not actually persisted/emailed — the view just shows a
  success message).
- **Response**: GET → render `voting/contact.html`. POST → success message + redirect to `contact`.

### `GET /store/` — Store front
- **View**: `store_view`
- **Auth**: none
- **Params (GET)**: `category` (string, optional) — filters `Product` by `category__name`.
- **Response**: HTML render `voting/store.html` with `products` (active only), `categories`, `selected_category`.

### `GET /tickets/` and `POST /tickets/` — Ticket lookup landing page
- **View**: `tickets_view`
- **Auth**: none
- **Params (POST)**: `action` = `"verify"` | `"retrieve"`.
  - `verify`: `reference` (string) — looked up against `TicketPurchase.paystack_reference` (case-insensitive via
    `.upper()`).
  - `retrieve`: `phone_or_ref` (string) — if it starts with `TK-`, treated as a reference; otherwise treated as a
    phone number and matched against the synthetic USSD buyer email `<phone>@ussd.vote`.
- **Behavior**: `verify` sets `ticket_found`/`error_message` in context. `retrieve`, on a successful match, redirects
  to `ticket_success` with the reference as a query param; otherwise sets `error_message`.
- **Response**: HTML render `voting/tickets.html`, or a redirect for a successful `retrieve`.

### `GET /event/<int:event_id>/tickets/` — Event's ticket types
- **View**: `event_tickets_view`
- **Auth**: none
- **Response**: HTML render `voting/event_tickets.html` with the event and its `is_active=True` tickets.

### `GET /verify-ticket/` and `POST /verify-ticket/` — Standalone ticket verification
- **View**: `verify_ticket_view`
- **Auth**: none
- **Params (POST)**: `reference` (string, upper-cased) matched against `TicketPurchase.paystack_reference`.
- **Response**: HTML render `voting/verify_ticket.html` with `ticket_found` or `error_message`.

### `GET /retrieve-ticket/` and `POST /retrieve-ticket/` — Standalone ticket retrieval
- **View**: `retrieve_ticket_view`
- **Auth**: none
- **Params (POST)**: `phone_or_ref` (string) — same `TK-` prefix vs. phone-number heuristic as `tickets_view`.
- **Behavior**: On success, redirects to `/ticket/success/?reference=<ref>`; otherwise renders with `error_message`.
- **Response**: HTML render `voting/retrieve_ticket.html`, or redirect.

---

## 2. Auth

### `GET /login/` and `POST /login/`
- **View**: `login_view`
- **Auth**: none (this *is* the login endpoint)
- **Rate limiting**: per-IP cache counter `login_attempts_<ip>`; blocks with an error message (still HTTP 200,
  renders the login page) once 5 attempts are recorded within a 60-second cache window.
- **Params (POST)**: `username` (string), `password` (string).
- **Behavior**: `authenticate()` + `login()` on success (clears the attempt counter), redirect to `home`. On
  failure, increments the attempt counter (60s TTL) and re-renders with an error message.
- **Response**: redirect to `home` on success; HTML render `voting/login.html` otherwise.

### `GET /register/` and `POST /register/`
- **View**: `register_view`
- **Auth**: none
- **Rate limiting**: per-IP cache counter `register_attempts_<ip>`, capped at 3 attempts / 60s.
- **Params (POST)**: `username`, `email`, `password` (all strings, all read via `request.POST.get`, no explicit
  validation beyond the username-uniqueness check).
- **Behavior**: Rejects if username taken. Otherwise creates a `User` + `Profile` (organizer awaiting approval),
  logs the new user in, and best-effort emails all superusers with an HTML notification (`fail_silently=True`,
  wrapped in `try/except`, so email failures never break registration).
- **Response**: HTML render `voting/register.html` (success or error message; no redirect on success — user stays
  on the page, now authenticated).

### `GET /logout/`
- **View**: `logout_view`
- **Auth**: none (safe regardless of session state)
- **Behavior**: `logout(request)`.
- **Response**: redirect to `home`.

---

## 3. Organizer Dashboard & Event Management

All endpoints in this section require `@login_required` (redirect to `/login/` if anonymous) and, unless noted, an
authorization check of `request.user == event.organizer or request.user.is_staff` — failing that check redirects to
`event_detail` (i.e. it fails "soft," not with a 403).

### `GET /dashboard/` — Dashboard
- **View**: `dashboard`
- **Auth**: login required. Non-staff users must have `profile.is_approved_organizer`, else redirected to `home`
  with an error message.
- **Behavior**: Staff see every `Event` and the 5 most recent platform-wide `ActivityLog` entries; organizers see
  only their own events and their own activity log entries.
- **Response**: HTML render `voting/dashboard.html`.

### `GET /dashboard/create/` and `POST /dashboard/create/` — Create event
- **View**: `create_event`
- **Auth**: login required + (`is_approved_organizer` or staff), else redirect to `home`.
- **Params (POST)**: `title`, `description`, `voting_mode`, `code_voting_mode` (default `'Standard'`),
  `enable_tie_breaker` (checkbox, `'on'`), `start_date_date` + `start_date_time` (combined into a datetime string
  and parsed), `end_date_date` + `end_date_time` (same), `platform_fee_percentage`, `primary_color`, `accent_color`,
  `background_image` (file), `event_image` (file).
- **Behavior**: Creates the `Event` with `organizer=request.user`, logs an `ActivityLog` entry.
- **Response**: redirect to `dashboard`. GET → HTML render `voting/create_event.html`.

### `GET /event/<int:event_id>/edit/` and `POST` — Edit event
- **View**: `edit_event` — same field set as `create_event`, applied to the existing `Event`. `background_image`/
  `event_image` only overwritten if present in `request.FILES`.
- **Response**: redirect to `dashboard` on POST; HTML render `voting/edit_event.html` on GET.

### `GET /event/<int:event_id>/analytics/` — Event analytics
- **View**: `event_analytics`
- **Auth**: organizer/staff of event only.
- **Behavior**: Per-category chart data (candidate names, main-vote counts, tie-breaker counts) plus a flat
  candidate list annotated with `main_votes`, `tie_breaker_votes`, and `revenue` (sum of successful transaction
  amounts).
- **Response**: HTML render `voting/analytics.html`.

### `POST /event/<int:event_id>/add-candidate/` — Add candidate
- **View**: `add_candidate`
- **Params (POST)**: `name`, `bio`, `image` (file), `category` (Category id, optional), `nominee_code`.
- **Behavior**: Creates `Candidate`, logs activity. No output/error surfaced if creation fails partway (no
  try/except); `category` lookup uses `Category.objects.get` which raises `DoesNotExist` uncaught if an invalid id
  is supplied.
- **Response**: redirect to `event_detail`.

### `GET /candidate/<int:candidate_id>/edit/` and `POST` — Edit candidate
- **View**: `edit_candidate` — same fields as add, image only replaced if supplied.
- **Response**: redirect to `event_detail` on POST; HTML render `voting/edit_candidate.html` on GET.

### `POST /event/<int:event_id>/bulk-add/` — Bulk add candidates
- **View**: `bulk_add_candidates`
- **Params (POST)**: `bulk_names` (newline-delimited names), `bulk_category` (Category id, optional).
- **Behavior**: Creates one `Candidate` per non-blank line (nominee_code auto-generated by the model). Logs
  activity and success/error message.
- **Response**: redirect to `event_detail`.

### `POST /event/<int:event_id>/add-category/` — Add category
- **View**: `add_category` — `name` (string, required to actually create anything; silently no-ops if blank).
- **Response**: redirect to `event_detail`.

### `GET /category/<int:category_id>/edit/` and `POST` — Edit category
- **View**: `edit_category` — `name` (string).
- **Response**: redirect to `event_detail` on POST; HTML render `voting/edit_category.html` on GET.

### `POST /event/<int:event_id>/generate-codes/` — Generate voting codes
- **View**: `generate_codes`
- **Params (POST)**: either `identifiers` (newline-delimited student IDs — one `VotingCode` per line, each tied to
  that identifier) **or**, if `identifiers` is blank, `count` (integer, default 10) generic/anonymous codes. Codes
  are random 8-char uppercase-alnum strings, guaranteed unique via a `while ... exists()` retry loop.
- **Response**: redirect to `event_detail` with a success message stating the count generated.

### `GET /event/<int:event_id>/download-codes/` — Download voting codes CSV
- **View**: `download_codes`
- **Response**: `Content-Type: text/csv`, `Content-Disposition: attachment; filename="<event.title>_codes.csv"`.
  Columns: `Code, Student ID / Identifier, Status, Used At`. All string cells pass through `sanitize_csv_value`
  (CSV-macro-injection guard: prefixes a leading `'` if the value starts with `=`, `+`, `-`, or `@`).

### `POST /event/<int:event_id>/clear-codes/` — Clear voting codes
- **View**: `clear_codes` — deletes all `VotingCode` rows for the event.
- **Response**: redirect to `event_detail`.

### `POST /event/<int:event_id>/upload-csv/` — Bulk-generate codes from CSV
- **View**: `upload_student_csv`
- **Params (POST)**: `csv_file` (file, must end in `.csv` — checked via filename only, not content-type; no
  `None`-check before `.name`, so a request without the file will raise `AttributeError`).
- **Behavior**: Decodes as UTF-8, reads each row's first column as a student identifier, generates a unique code
  per non-blank row.
- **Response**: redirect to `event_detail` with success/error message.

### `GET /event/<int:event_id>/retrieve-code/` (not routed as GET-render — see below) — Retrieve a voting code
- **View**: `retrieve_voting_code`
- **Auth**: none (public — this is the self-service "forgot my code" flow; GET falls through to the redirect since
  there's no template render branch, it only acts on POST).
- **Params (POST)**: `student_id` (string), `email` (string).
- **Behavior**: Looks up `VotingCode` by `event` + `voter_identifier__iexact=student_id`. If found and unused,
  emails the code to the supplied `email` address (`send_mail`, `fail_silently=True`) and writes an `ActivityLog`
  entry recording the disclosure (attributed to the requesting user if authenticated, else `None`). If already
  used or not found, sets an error message instead.
- **Response**: redirect to `event_detail` (both GET and POST — there is no dedicated template).

---

## 4. Voting (pay-to-vote, code voting, tie-breaker)

### `POST /vote/<int:candidate_id>/` — Initiate a paid vote
- **View**: `initiate_vote`
- **Auth**: none for voters; explicitly **blocks** staff/approved-organizer accounts from voting (redirects to
  `event_detail` with an error).
- **Params (POST)**: `amount` (integer, number of votes = number of GHS to pay; must parse as int and be ≥ 1, else
  redirect back with an error/no-op).
- **Behavior**: Calls `initialize_paystack_payment(voter_email="anonymous@FlexyVotes.com", amount, candidate_id)`
  (see `voting/services.py`) which POSTs to Paystack's `/transaction/initialize` with a UUID reference and
  `callback_url = {SITE_URL}/vote/success/`. On success, creates a `VoteTransaction` (`status='Pending'`,
  `vote_type` defaults to `'Main'`) and redirects the browser to Paystack's `authorization_url`. On failure,
  redirects back to `event_detail`.
- **Response**: redirect (to Paystack checkout, or back to `event_detail` on error). Non-POST → redirect to `home`.

### `GET /vote/success/` — Paystack return URL for votes
- **View**: `vote_success`
- **Auth**: none
- **Params (GET)**: `reference` (string) — the Paystack transaction reference.
- **Behavior**: If a matching `VoteTransaction` is `Pending`, marks it `Success` (a client-side fallback in case the
  webhook hasn't landed yet — this is a trust-the-browser confirmation path, not authoritative). Always shows a
  success message.
- **Response**: redirect to `event_detail` (if reference resolves) or `home`.

### `POST /vote/code/<int:candidate_id>/` — Cast a vote using a code or ticket reference
- **View**: `cast_vote_with_code`
- **Auth**: none
- **Params (POST)**: `code` (string, upper-cased/stripped), `identifier` (string, optional — student ID for
  identifier-bound codes).
- **Behavior** — two flows based on the `code` prefix:
  1. **Ticket-reference tie-breaker vote** (`code` starts with `TK-`): looks up a `TicketPurchase` by
     `paystack_reference` + `event`. Requires: purchase exists, `status == 'Success'`, `has_voted == False`,
     `event.enable_tie_breaker == True`, and `purchase_method == 'Web'` (USSD-purchased tickets are excluded from
     the free vote). On success, creates a `VoteTransaction` (`vote_type='Tie-Breaker'`, `amount=0`,
     `number_of_votes = purchase.quantity`, synthetic reference `TIE_<code>_<random4>`), marks the ticket
     `has_voted=True`.
  2. **Standard voting code**: looks up `VotingCode` by `event` + `code`. If the code has a bound
     `voter_identifier`, the supplied `identifier` must case-insensitively match. Rejects already-used codes.
     Otherwise marks the code used and creates a `VoteTransaction` (`vote_type='Main'`, `amount=0`,
     `number_of_votes=1`, synthetic reference `TIE_<code>_<random4>`).
  Also rejects any vote if `timezone.now() > event.end_date` ("Voting for this event has ended").
- **Response**: redirect to `event_detail` in every case, with a success/error message.

### `GET /event/<int:event_id>/live-counts/` — Live vote counts (JSON API)
- **View**: `live_vote_counts`
- **Auth**: none
- **Method**: GET
- **Response**: `application/json`
  ```json
  {
    "candidates": [
      {"id": 12, "name": "Jane Doe", "votes": 340, "tie_breakers": 5, "percentage": 62}
    ],
    "total_votes": 548
  }
  ```
  `votes` = summed `Main` successful transactions, `tie_breakers` = summed `Tie-Breaker` successful transactions,
  `percentage` computed against `total_votes` (which itself is the sum of `votes` only, matching `event_detail`'s
  fairness rule). Candidates ordered by `votes` desc, `tie_breakers` desc, `name`.
- **Errors**: 404 if `event_id` doesn't exist.

---

## 5. USSD (Africa's Talking)

### `POST /ussd/callback/`
- **View**: `ussd_callback` — `@csrf_exempt` (external caller: Africa's Talking's gateway posts here directly).
- **Auth**: none — this is a public machine-callable protocol endpoint. Note it is registered **twice**: once in
  `vote_fund/urls.py` (top-level, evaluated first) and again in `voting/urls.py`; the top-level route wins.
- **Params (POST, form-encoded, per Africa's Talking's USSD spec)**: `sessionId`, `serviceCode`, `phoneNumber`,
  `text` (the accumulated `*`-delimited input string for the whole session — AT's gateway resends the full history
  each request, and the view derives the current step purely from `len(text.split('*'))`; there is no server-side
  session object).
- **Response content-type**: `text/plain`. Response body always starts with `CON` (continue — show another menu
  and wait for more input) or `END` (terminate the session).
- **Behavior — two flows selected by the first `*`-token**:
  - **`1` — Vote for a candidate**: step 1 asks for a nominee code → looked up via `Candidate.nominee_code`; step 2
    asks for vote quantity (1 GHS = 1 vote, no payment gateway involved — USSD votes are recorded as immediately
    `status='Success'`, `paystack_reference = "USSD_<random8>"`, no real Paystack charge is initiated); step 3 asks
    to confirm; step 4 creates the `VoteTransaction` and ends with a confirmation message, or "Transaction
    cancelled" if the user chose Cancel.
  - **`2` — Buy an event ticket**: a 6-step wizard — pick event (only `is_active=True` events with ≥1 ticket type)
    → pick ticket type → enter quantity → enter buyer name → confirm → on confirm, creates a `TicketPurchase` with
    `status='Success'` directly (again, no real payment gateway call), `purchase_method='USSD'`, and a synthetic
    reference `TK-<2 letters><4 digits>`. All the list/selection steps are re-derived from scratch each request by
    re-slicing `Event`/`Ticket` querysets by numeric index from `inputs[]` — if the underlying event/ticket list
    changes between USSD screens (e.g., another purchase or an admin edit shifts ordering) the selected index can
    resolve to the wrong record.
  - Any unrecognized `first_input`, or a level not handled, ends with `"END Invalid request."`.
- **Example — voting session** (4 round trips as the USSD gateway would send them):
  ```
  1) POST text=""            -> "CON Welcome to FlexyVotes.\n1. Vote for Candidate\n2. Buy Event Ticket"
  2) POST text="1"           -> "CON Enter Nominee Code:"
  3) POST text="1*NOM123"    -> "CON You selected Jane Doe.\nEnter number of votes (1 GHS = 1 vote):"
  4) POST text="1*NOM123*5"  -> "CON Pay GHS 5 for 5 votes for Jane Doe?\n1. Confirm\n2. Cancel"
  5) POST text="1*NOM123*5*1"-> "END Payment successful! You have cast 5 votes for Jane Doe."
  ```
  Example request body for step 5 (form-encoded, as Africa's Talking sends it):
  ```
  sessionId=ATUid_abc123&serviceCode=*384*1234%23&phoneNumber=%2B233241234567&text=1*NOM123*5*1
  ```
  Example response: `HTTP 200`, `Content-Type: text/plain`, body `END Payment successful! You have cast 5 votes for Jane Doe.`

---

## 6. Ticketing

### `GET /event/<int:event_id>/create-ticket/` and `POST` — Create ticket type
- **View**: `create_ticket`
- **Auth**: login required + organizer/staff of event.
- **Params (POST)**: `name`, `price`, `quantity_available`, `image` (file), `old_price` (optional).
- **Response**: redirect to `event_detail` on POST (success message); HTML render `voting/create_ticket.html` on GET.

### `POST /buy-ticket/<int:ticket_id>/` — Buy a ticket (initiates payment)
- **View**: `buy_ticket`
- **Auth**: none
- **Params (POST)**: `name` (buyer name), `email` (buyer email), `quantity` (integer, default 1, must be ≥ 1).
- **Behavior**: Validates quantity against remaining stock (`ticket.quantity_available` minus already-`Success`
  purchases). Calls `initialize_ticket_payment` (generates a `TK-XXNNNN` reference, Paystack metadata
  `type: "ticket_purchase"`), creates a `TicketPurchase` with `status='Pending'`, redirects to Paystack checkout.
- **Response**: redirect to Paystack auth URL, or back to `event_tickets`/`tickets` with an error message.
  Non-POST → redirect to `tickets`.

### `GET /ticket/success/` — Paystack return URL for ticket purchases
- **View**: `ticket_success`
- **Auth**: none
- **Params (GET)**: `reference` (string).
- **Behavior**: If `Pending`, marks the purchase `Success` (client-confirmation fallback, same pattern as
  `vote_success`) and sets `just_paid=True`. Generates a QR code (PNG, base64-inlined) encoding event/buyer/ticket
  info and the reference — used by the template for the e-ticket display and later re-sent by
  `send_ticket_email`.
- **Response**: HTML render `voting/ticket_success.html` with `purchase`, `qr_code_base64`, `just_paid`. Redirects
  to `home` if no reference given or no matching purchase.

### `POST /ticket/send-email/` — Resend e-ticket via email (JSON body)
- **View**: `send_ticket_email` — `@csrf_exempt` (public, unauthenticated; called from the ticket-success page's
  JS with the client-rendered QR image, so it cannot carry a session CSRF token reliably).
- **Auth**: none.
- **Rate limiting**: per-IP cache counter `send_ticket_email_<ip>`, max 10 requests/60s → `HTTP 429` once exceeded.
- **Request body**: JSON, `Content-Type: application/json` expected:
  ```json
  {"reference": "TK-AB1234", "image": "data:image/png;base64,iVBORw0K..."}
  ```
  - `reference` (string, required) — must resolve to an existing `TicketPurchase`.
  - `image` (string, required) — a data URI; parsed as `<header>;base64,<data>`. `header` must end in a MIME
    subtype found in `ALLOWED_TICKET_EMAIL_IMAGE_TYPES = {png, jpeg, jpg}` (case-insensitive). Decoded bytes must
    be ≤ `MAX_TICKET_EMAIL_IMAGE_BYTES = 5 MiB`.
- **Behavior**: If the purchase's buyer email is a synthetic `...@ussd.vote` address (i.e. purchased via USSD, no
  real email on file), returns `200` without sending anything ("pretend success" to avoid client-side errors).
  Otherwise sends an `EmailMessage` with the decoded image attached as `ticket_<reference>.<ext>`,
  `fail_silently=True`.
- **Responses**:
  - `200` — accepted (email sent, or silently skipped for USSD buyers). Body is empty (`HttpResponse(status=200)`).
  - `400` — non-POST method; unparseable JSON; missing `reference`/`image`/matching purchase; malformed data URI;
    disallowed image type; oversized image.
  - `429` — rate limit exceeded.
- **Note**: send failures inside `email.send(fail_silently=True)` are swallowed — a `200` does not guarantee actual
  delivery.

### `GET /event/<int:event_id>/guestlist/` — View guest list
- **View**: `event_guestlist`
- **Auth**: login required + organizer/staff of event.
- **Response**: HTML render `voting/guestlist.html` listing all `Success` `TicketPurchase` rows for the event.

### `GET /event/<int:event_id>/download-guestlist/` — Download guest list CSV
- **View**: `download_guestlist`
- **Auth**: login required + organizer/staff of event.
- **Response**: `text/csv`, `Content-Disposition: attachment; filename="<event.title>_guestlist.csv"`. Columns:
  `Buyer Name, Buyer Email, Ticket Type, Quantity, Reference, Purchased At`. String fields pass through
  `sanitize_csv_value`.

### `GET /ticket/<int:ticket_id>/edit/` and `POST` — Edit ticket type
- **View**: `edit_ticket` — `name`, `price`, `old_price`, `quantity_available`, `is_active` (checkbox), `image`
  (optional file replace).
- **Response**: redirect to `event_detail` on POST; HTML render `voting/edit_ticket.html` on GET.

### `POST /ticket/<int:ticket_id>/delete/` — Delete ticket type
- **View**: `delete_ticket` — auth: organizer/staff of event; deletes the `Ticket` row outright.
- **Response**: redirect to `event_detail`.

---

## 7. Store

### `GET /dashboard/store/` — Manage store (admin)
- **View**: `manage_store`
- **Auth**: login required + `request.user.is_staff` (not organizer — staff-only), else redirect to `home`.
- **Response**: HTML render `voting/manage_store.html` listing all `Product`s and `ProductCategory`s.

### `GET /dashboard/store/add/` and `POST` — Add product
- **View**: `add_product`
- **Auth**: staff only.
- **Params (POST)**: `name`, `description`, `price`, `old_price` (optional), `image` (file), `category`
  (ProductCategory id, optional), `is_active` (checkbox).
- **Response**: redirect to `manage_store` on POST; HTML render `voting/add_product.html` on GET.

### `GET /dashboard/store/edit/<int:product_id>/` and `POST` — Edit product
- **View**: `edit_product` — staff only; same fields as add, image replaced only if supplied.
- **Response**: redirect to `manage_store` on POST; HTML render `voting/edit_product.html` on GET.

---

## 8. Scanner / Check-in (JSON API)

### `GET /event/<int:event_id>/scanner/` — Scanner UI page
- **View**: `event_scanner`
- **Auth**: login required + organizer/staff of event.
- **Response**: HTML render `voting/scanner.html` (the page hosting the camera/QR-scan JS that calls
  `process-scan` below).

### `POST /event/<int:event_id>/process-scan/` — Process a scanned ticket QR (JSON API)
- **View**: `process_scan`
- **Auth**: `@login_required`, **and** organizer/staff-of-that-event check — unauthorized requests get
  `HTTP 403` JSON (not a redirect, since this is an API endpoint consumed by JS). This authorization check was
  recently added/fixed.
- **CSRF**: **not** `@csrf_exempt` — the calling JS must send the standard Django CSRF token (e.g. via the
  `X-CSRFToken` header sourced from the `csrftoken` cookie or a page-embedded token).
- **Request body**: JSON, `Content-Type: application/json`:
  ```json
  {"text": "EVENT: Prom Night\nNAME: John Smith\nTICKET: VIP\nQTY: 2\nREF: TK-AB1234"}
  ```
  `text` is the raw multi-line string decoded from the ticket's QR code (as produced by `ticket_success`'s QR
  generator). The view scans line-by-line for a line starting with `REF:` and extracts everything after it as the
  reference.
- **Behavior**: Looks up `TicketPurchase` scoped to `event_id` + the extracted reference. Rejects if not found for
  this event, if payment isn't `Success`, or if already checked in. On a valid, un-used, paid ticket: sets
  `is_checked_in=True` and `checked_in_at=now()`.
- **Responses** (all `application/json`):
  - `403` — `{"status": "error", "message": "Not authorized for this event."}`
  - `400` (invalid JSON body) — `{"status": "error", "message": "Invalid request body."}`
  - `400` (no `REF:` line found) — `{"status": "error", "message": "Invalid QR Code (No reference found)."}`
  - `404` (no matching ticket for this event) — `{"status": "error", "message": "Ticket not found for this event."}`
  - `400` (payment not successful) — `{"status": "error", "message": "Payment pending or failed."}`
  - `409` (already checked in) — `{"status": "error", "message": "ALREADY USED! Checked in at <time> by <name>."}`
  - `200` (success) — `{"status": "success", "message": "Welcome, <name>! <qty> <ticket type> ticket(s)."}`
  - `400` (non-POST method) — `{"status": "error", "message": "Invalid request."}`

---

## 9. Webhooks / Callbacks

### `POST /webhook/paystack/` — Paystack payment webhook
- **View**: `paystack_webhook` — `@csrf_exempt` (external caller: Paystack's servers).
- **Auth**: none via Django auth — authenticity is instead verified via HMAC signature.
- **Signature verification**: header `X-Paystack-Signature` must equal `HMAC-SHA512(PAYSTACK_SECRET_KEY,
  raw_request_body)` (hex digest), compared with `hmac.compare_digest`. Mismatch → `HTTP 400` immediately, no body
  is parsed.
- **Request body**: JSON, Paystack's standard webhook envelope. The view only acts on `event == "charge.success"`:
  ```json
  {
    "event": "charge.success",
    "data": {
      "reference": "TK-AB1234",
      "metadata": {"type": "ticket_purchase", "ticket_id": 7, "quantity": 2}
    }
  }
  ```
- **Behavior**:
  - If `data.metadata.type == "ticket_purchase"`: looks up `TicketPurchase` by `paystack_reference`; if `Pending`,
    marks `Success`. `DoesNotExist` is swallowed silently.
  - Otherwise: looks up `VoteTransaction` by `paystack_reference`; if `Pending`, marks `Success`. `DoesNotExist`
    swallowed silently.
  - Any other `event` value: no-op, but still returns `200` (so Paystack doesn't retry).
- **Responses**:
  - `400` — bad/missing signature; unparseable JSON body; non-POST method.
  - `200` — signature verified and body processed (regardless of whether a matching record was found — this is
    Paystack's expected "ack" response so it doesn't retry the webhook).
- **Example request** (illustrative — actual signature must be computed over the exact raw body bytes):
  ```
  POST /webhook/paystack/ HTTP/1.1
  Content-Type: application/json
  X-Paystack-Signature: 6f1f7c2c9c...  (hex SHA-512 HMAC of the body below)

  {"event":"charge.success","data":{"reference":"TK-AB1234","metadata":{"type":"ticket_purchase","ticket_id":7,"quantity":2}}}
  ```
  **Example response**: `HTTP/1.1 200 OK` (empty body).

### `POST /ussd/callback/` — Africa's Talking USSD callback
- See section 5 above for full details, params, and a worked example. Reiterated here because it is, functionally,
  a webhook/callback endpoint from an external telco aggregator: `@csrf_exempt`, plain-text CON/END protocol
  (not JSON), driven entirely by the accumulated `text` field with no server-side session storage.

---

## Appendix: Endpoints not covered above (grouped for completeness)

| Method | Path | View | Section |
|---|---|---|---|
| GET | `/admin/` | Django admin site | out of scope (framework-provided) |

All other paths in `voting/urls.py` / `vote_fund/urls.py` are documented in the sections above.

## Appendix: Cross-cutting notes

- **CSV export sanitization**: `download_codes` and `download_guestlist` both pass every string cell through
  `sanitize_csv_value`, which prefixes a leading single-quote if the value starts with `=`, `+`, `-`, or `@`,
  mitigating CSV/Excel formula-injection when the exports are opened in spreadsheet software.
- **"Success via redirect" pattern**: `vote_success` and `ticket_success` both optimistically flip a `Pending`
  transaction/purchase to `Success` when the user's browser lands back on the success page with a `reference`
  query param — this is a fallback for local/dev environments or race conditions where Paystack's webhook hasn't
  arrived yet. The webhook (`paystack_webhook`) remains the authoritative confirmation path in production.
- **Synthetic email addresses**: USSD-originated votes and ticket purchases store `<phone>@ussd.vote` as the
  "email" so the same `VoteTransaction`/`TicketPurchase` models work for both channels. `send_ticket_email`
  explicitly detects and no-ops on these addresses rather than attempting delivery.
- **Authorization failure style is inconsistent by design**: HTML views generally fail authorization by silently
  redirecting to `event_detail` (no 403), whereas the two JSON API endpoints (`process_scan`, and implicitly
  `send_ticket_email`/`live_vote_counts` which have no auth at all) return proper HTTP status codes since they're
  consumed by JavaScript, not a browser navigation.
