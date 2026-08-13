# FlexyVotes — Product Requirements Document

*Grounded strictly in the implemented Django codebase (`voting` app) as of this writing. Where a claim needs code evidence, the relevant file/function is cited.*

---

## 1. Purpose / Problem Statement

FlexyVotes is a Django-based web platform (with a USSD channel) targeted at Ghana that lets event organizers run **donation-based or code-based voting competitions** (awards shows, pageants, talent contests, school elections), **sell event tickets** with QR e-tickets, and **sell small merchandise** (plaques, branded items) — all in one place.

The core problem it solves: organizers in Ghana who run "vote for your favorite" fundraising competitions (church, school, community, entertainment) currently rely on manual mobile-money collection and spreadsheets to tally votes, issue tickets, and prevent double-voting or duplicate ticket use. FlexyVotes digitizes this with:

- **Pay-to-Vote**: monetized voting where GHS amount paid maps directly to vote count (`amount * 1 = votes`), processed via Paystack, turning voter engagement into fundraising revenue for the organizer (minus a platform fee).
- **Code Voting**: free, one-code-one-vote elections (with optional Student ID binding) for fair, non-monetized contests such as school elections.
- **USSD access**: voting and ticket purchase for feature-phone/no-data users via Africa's Talking-style USSD flow (no ticket-code lookup for voting; ticket purchase and retrieval supported).
- **Ticketing**: sell tickets to the underlying event, issue QR-coded e-tickets by email, and check attendees in via an organizer-facing camera/manual QR scanner.
- **Store**: a small public merch storefront (plaques etc.) for the platform (not per-organizer).

## 2. Target Users / Personas

| Persona | Description | Auth | Key code touch-points |
|---|---|---|---|
| **Anonymous Voter / Web Ticket Buyer** | The public. Browses events, votes (pays or enters a code), buys tickets on the web. No account needed. | None | `home`, `event_detail`, `initiate_vote`, `cast_vote_with_code`, `buy_ticket`, `tickets_view` |
| **USSD User** | Feature-phone user dialing a USSD short code. Can vote for a candidate by nominee code (Pay-to-Vote only, GHS1 = 1 vote) or buy an event ticket, then retrieve the ticket later via phone number or reference. | None (identified by phone number) | `ussd_callback`, `retrieve_ticket_view` |
| **Organizer** | Self-registers on the site; must be approved by an admin (`Profile.is_approved_organizer`) before they can create events. Once approved: creates/edits events, categories, candidates, tickets; generates voting codes; views analytics/guest lists; operates the check-in scanner for their own events. | Django `User` + `Profile` | `register_view`, `dashboard`, `create_event`, `add_candidate`, `add_category`, `generate_codes`, `event_analytics`, `event_scanner`, `event_guestlist` |
| **Platform Admin** | Django staff/superuser. Approves or unapproves organizers (Django admin action with email notification), sees **all** events (not just their own) on the dashboard, can manage/edit any event, manages the platform-wide Product store, and can act as an organizer on any event (bypasses the organizer-ownership checks throughout `views.py`). | Django staff/superuser | `voting/admin.py` (`CustomUserAdmin.approve_organizers`), `dashboard` (branches on `request.user.is_staff`), every `if request.user != event.organizer and not request.user.is_staff` guard |

Note: organizers and admins are explicitly blocked from voting on their own events (`initiate_vote` checks `is_staff` / `is_approved_organizer` and rejects with "Admins and Organizers cannot vote.").

## 3. Core User Flows (as implemented)

### 3.1 Organizer self-registration & approval
1. User visits `/register/` (`register_view`), submits username/email/password. Rate-limited to 3 attempts/minute per IP via Django cache.
2. `User` + `Profile` (with `is_approved_organizer=False`) are created; the user is logged in immediately.
3. An email is sent to every superuser (`User.objects.filter(is_superuser=True)`) notifying them of the pending registration (best-effort, `fail_silently=True`).
4. The homepage shows a "Account Created — awaiting admin confirmation" modal (`show_registration_popup` session flag).
5. An admin opens Django Admin → Users, selects the user, runs the **"Approve selected as Organizers"** action (`CustomUserAdmin.approve_organizers`), which flips `Profile.is_approved_organizer = True` and emails the organizer an approval notice.
6. Only after approval can the user reach `/dashboard/create/` (`create_event` redirects unapproved, non-staff users to `home`).

### 3.2 Event creation (organizer/admin)
1. Approved organizer (or staff) opens `/dashboard/create/` → `create_event.html` form.
2. Organizer sets: title, description, start/end date+time, `voting_mode` (Pay to Vote / Code Voting), `code_voting_mode` (Standard / Student ID) — only relevant if Code Voting — `enable_tie_breaker` checkbox, `platform_fee_percentage`, theme `primary_color`/`accent_color`, optional `background_image` and `event_image` (both validated ≤2MB).
3. On submit, `Event.objects.create(...)` is called with `organizer=request.user`; an `ActivityLog` entry "Created event '<title>'" is written.
4. Organizer is redirected to `/dashboard/`, where the new event appears in their table with computed Revenue (₵) and Payout (₵) columns (`Event.get_total_revenue`, `Event.get_organizer_payout`).
5. From the event detail page, the organizer (or staff) can then: add Categories, add/bulk-add Candidates (with auto-generated `nominee_code` if left blank — a 2-letter+3-digit code, e.g. "TE025"), create Ticket types, and — if Code Voting — generate/upload voting codes.

### 3.3 Pay-to-Vote flow (web)
1. Voter opens `/event/<id>/`, clicks "Vote for me" on a candidate, enters a GHS amount (≥1) in the inline form.
2. `initiate_vote` blocks the request if the requester is staff or an approved organizer.
3. `initialize_paystack_payment` (in `services.py`) calls Paystack's `/transaction/initialize` with `amount * 100` (kobo/pesewas), a UUID reference, and metadata `{candidate_id, voter_email}`; `voter_email` defaults to a hard-coded placeholder `anonymous@FlexyVotes.com` (no real email is actually collected for pay-to-vote).
4. A `VoteTransaction` is created immediately with `status='Pending'`, `vote_type='Main'`, `number_of_votes = amount` (1 GHS = 1 vote), tied to the Paystack reference; the voter is redirected to the Paystack checkout URL.
5. Confirmation happens two ways: (a) Paystack's server-to-server webhook `POST /webhook/paystack/` (`paystack_webhook`) verifies the HMAC-SHA512 signature and, on `charge.success`, flips the matching `VoteTransaction.status` to `Success`; (b) the browser is redirected back to `/vote/success/?reference=...` (`vote_success`), which also flips `Pending → Success` as a local-dev fallback, then redirects to the event page with a success message.
6. Vote counts (`event_detail`, `live_vote_counts`) only sum transactions with `status='Success'`, so pending/failed payments never count.

### 3.4 Code Voting flow (incl. Student ID and Tie-Breaker)
1. Organizer sets `voting_mode='Code Voting'`. If `code_voting_mode='Student ID'`, codes are bound to a student identifier; otherwise codes are anonymous.
2. Organizer generates codes via `/event/<id>/generate-codes/` either by pasting a list of identifiers (one `VotingCode` per identifier) or specifying a count of anonymous codes; or via `/event/<id>/upload-csv/` (`upload_student_csv`) which reads a CSV's first column as identifiers. Codes are unique 8-char alphanumeric strings (`generate_voting_code`/ad hoc generation). Organizer can `download_codes` (CSV export, CSV-injection-sanitized) or `clear_codes` (delete all).
3. Student-ID mode voters who lost their code can self-serve retrieval at `/event/<id>/retrieve-code/` (`retrieve_voting_code`): enter Student ID + email; if a matching unused `VotingCode` exists, the code is emailed to them and an `ActivityLog` entry records the retrieval (for audit/anti-abuse).
4. To vote: voter opens candidate's "Cast Vote" form, enters (Student ID if required) + code. `cast_vote_with_code`:
   - Rejects if the event has ended (`end_date` passed).
   - If the code (uppercased) matches a `VotingCode` for that event: verifies identifier match (if bound), verifies `is_used=False`, marks it used with `used_at=now`, and creates a `VoteTransaction` with `amount=0`, `vote_type='Main'`, `number_of_votes=1`.
   - Otherwise reports "Invalid voting code."
5. **Tie-Breaker via ticket reference**: if the entered code starts with `TK-` (a Paystack ticket reference format), the view treats it as a ticket-based free vote instead of a code lookup:
   - Requires: a `TicketPurchase` exists for that reference *and* event; its `status == 'Success'`; it has not already voted (`has_voted=False`); the event has `enable_tie_breaker=True`; and the purchase's `purchase_method == 'Web'` (USSD-bought tickets are explicitly excluded from this free-vote perk).
   - On success, creates a `VoteTransaction` with `vote_type='Tie-Breaker'`, `amount=0`, and `number_of_votes = ticket_purchase.quantity` (i.e., a ticket bought in bulk yields multiple tie-breaker votes), then sets `ticket_purchase.has_voted=True` so the same ticket can't be reused.
   - This mechanic also works in Pay-to-Vote events when `enable_tie_breaker=True` — the event UI shows a toggle button ("Cast Free Vote") on the pay form that swaps to a ticket-reference input, reusing the same `cast_vote_with_code` endpoint.
6. Main votes and Tie-Breaker votes are tracked and displayed **separately** in `event_detail`/`live_vote_counts`/`event_analytics`, but the public leaderboard percentage/sort in `event_detail` is currently based on Main votes only (`vote_count`), with Tie-Breaker count shown alongside but not folded into the ranking percentage displayed to voters.

### 3.5 USSD voting flow
Implemented in `ussd_callback` as a single stateless endpoint driven by Africa's Talking-style `text`-accumulation (`*`-separated) session state. No database session object is created for USSD navigation — each step's context is re-derived from parsing `inputs`.

1. Dial in → Main Menu: `1. Vote for Candidate` / `2. Buy Event Ticket`.
2. **Voting** (`1`): enter Nominee Code → app looks up `Candidate.nominee_code`. If found, prompt for number of votes (GHS1 = 1 vote). Confirm payment (`1. Confirm / 2. Cancel`). On confirm, a `VoteTransaction` is created directly with `status='Success'` (no real Paystack/mobile-money charge is executed in this code path — it's marked paid outright), `voter_email = "<phone>@ussd.vote"`, reference `USSD_<hex8>`.
3. USSD voting only supports Pay-to-Vote by nominee code; there is no USSD path for entering a Code-Voting voting code or Student ID.

### 3.6 USSD ticket purchase & retrieval flow
1. Menu option `2` walks the caller through: select event (only events with at least one ticket type) → select ticket type → enter quantity → enter full name → confirm total cost.
2. On confirm, a `TicketPurchase` is created directly with `status='Success'` (again, no real payment gateway call — USSD "payment" is simulated as instantly successful), `purchase_method='USSD'`, `buyer_email = "<phone>@ussd.vote"`, and a reference in the `TK-XXNNNN` format (2 letters + 4 digits).
3. The USSD response tells the buyer to visit `FlexyVotes.com/retrieve` to view their ticket.
4. On the web, `/retrieve-ticket/` (`retrieve_ticket_view`) or the "Retrieve USSD Ticket" panel on `/tickets/` (`tickets_view`, action=`retrieve`) accepts either the `TK-` reference or the phone number; phone lookups reconstruct the synthetic `<phone>@ussd.vote` email to find the matching `TicketPurchase`, then redirect to the same `ticket_success` page a web buyer would see (including the QR code) if `status == 'Success'`.

### 3.7 Ticket purchase (web) + QR e-ticket + email + check-in flow
1. Voter/attendee browses `/tickets/` or `/event/<id>/tickets/` (`event_tickets_view`), sees active ticket types with price/old-price discount badge.
2. Submits name, email, quantity on `buy_ticket`. Server checks `already_sold + quantity <= ticket.quantity_available` (aggregating `Success` purchases) before allowing the purchase.
3. `initialize_ticket_payment` generates a `TK-<2 letters><4 digits>` reference and calls Paystack `/transaction/initialize` with metadata `{type: 'ticket_purchase', ticket_id, quantity}`; a `TicketPurchase` row is created with `status='Pending'`, `purchase_method='Web'` (default), and the buyer is redirected to Paystack checkout.
4. Paystack webhook (`paystack_webhook`) inspects `metadata.type == 'ticket_purchase'` and flips the matching `TicketPurchase.status` to `Success` on `charge.success`. The browser-side redirect to `/ticket/success/?reference=...` (`ticket_success`) also flips `Pending → Success` as a fallback and sets `just_paid=True` for that request.
5. `ticket_success` generates a QR code (via the `qrcode` library) embedding structured plain text (`EVENT`, `NAME`, `TICKET`, `QTY`, `REF:` lines) and renders it as a base64 PNG in `ticket_success.html`, alongside a "branded" e-ticket view.
6. The client-side JS on `ticket_success.html` can POST a rendered ticket image to `/ticket/send-email/` (`send_ticket_email`), which emails the PNG/JPEG as an attachment to `buyer_email` via `EmailMessage` — but only if the email is not a synthetic `@ussd.vote` address (USSD buyers have no real email and this is explicitly skipped, returning 200 to avoid client errors). This endpoint is CSRF-exempt but IP-rate-limited (10/min) and validates image type/size (PNG/JPEG ≤5MB).
7. At the event, the organizer opens `/event/<id>/scanner/` (`event_scanner`, organizer/staff only) — a camera-based QR scanner (`html5-qrcode` JS library) with a manual-entry fallback. Scanned/typed text is parsed for a `REF:` line and POSTed to `/event/<id>/process-scan/` (`process_scan`), which:
   - 403s if the requester isn't the organizer/staff.
   - 404s if no `TicketPurchase` exists for that event+reference.
   - 400s if payment isn't `Success`.
   - 409s ("ALREADY USED!") with the original check-in time/name if `is_checked_in=True`.
   - Otherwise sets `is_checked_in=True`, `checked_in_at=now`, and returns a success JSON with a welcome message including buyer name, quantity, and ticket type.
8. Organizers can view (`event_guestlist`) and export (`download_guestlist`, CSV-injection-sanitized) the list of all `Success` `TicketPurchase` rows for their event, and independently verify any ticket by reference on the public `/verify-ticket/` page (`verify_ticket_view`) without check-in side effects.

### 3.8 Store browsing
1. Public `/store/` (`store_view`) lists active `Product`s, optionally filtered by `ProductCategory` name via `?category=`.
2. This is a **platform-wide** store (not per-event, not per-organizer) — products are only manageable by staff via `/dashboard/store/` (`manage_store`, `add_product`, `edit_product`), all gated on `request.user.is_staff`.
3. Products show price, optional `old_price` with a computed `discount_percentage`, image, description. There is no cart, checkout, or payment flow for products in the code — it is a display-only catalogue (browsing only; no purchase path exists for merch).

## 4. Functional Requirements (by area)

### 4.1 Events & Candidates
- FR-1: An `Event` has a `voting_mode` (Pay to Vote | Code Voting), and if Code Voting, a `code_voting_mode` (Standard | Student ID).
- FR-2: An `Event` has a boolean `enable_tie_breaker` that unlocks the ticket-reference free-vote mechanic.
- FR-3: An `Event` has theme fields (`primary_color`, `accent_color` hex strings) and optional `background_image`/`event_image` (≤2MB each), applied live in `event_detail.html` inline styles.
- FR-4: An `Event` has a `platform_fee_percentage` (default 20.00%) used to compute `get_organizer_payout()` = total successful revenue minus that percentage.
- FR-5: Events belong to exactly one `organizer` (`User`, nullable on delete); `is_active` flag controls homepage listing (`home` only lists `is_active=True`).
- FR-6: `Category` groups `Candidate`s within an event (e.g., "Best Female Artist"); candidates may be uncategorized.
- FR-7: `Candidate.nominee_code` auto-generates a unique 2-letter+3-digit code on save if left blank, used for USSD lookup and displayed as a badge on the event page.
- FR-8: Organizers can add candidates individually (with image, bio, category, optional manual nominee code) or in bulk via a newline-separated textarea (`bulk_add_candidates`), with categories optionally assigned to the whole batch.
- FR-9: Only the event's `organizer` or a staff user may: edit the event, add/edit categories, add/edit/bulk-add candidates, create/edit/delete tickets, generate/clear/download voting codes, upload student CSVs, view analytics, view/download the guest list, or use the scanner. All of these are enforced per-request (`request.user != event.organizer and not request.user.is_staff`), not via Django permissions/groups.

### 4.2 Voting
- FR-10: Pay-to-Vote: 1 GHS paid = 1 vote (`number_of_votes = amount`); amount is a whole-number GHS entry validated server-side as an integer ≥1.
- FR-11: Payment confirmation is dual-path: Paystack webhook (signature-verified via HMAC-SHA512 against `PAYSTACK_SECRET_KEY`) and a same-request fallback on the checkout return URL.
- FR-12: Organizers/staff cannot cast votes on any event (enforced in `initiate_vote`).
- FR-13: Voting is blocked once `timezone.now() > event.end_date` (enforced explicitly in `cast_vote_with_code`; the pay-to-vote form itself does not appear to re-check expiry server-side in `initiate_vote`, only in the UI where `is_expired` disables the button).
- FR-14: Code Voting: one code = one use; codes may optionally be bound to a Student ID that must match exactly (case-insensitive) at cast time.
- FR-15: Tie-Breaker free votes require a *successful, web-purchased, not-yet-used-to-vote* ticket for the *same event*; the number of votes granted equals the ticket's purchased quantity; each ticket can grant its free vote only once (`has_voted` flag).
- FR-16: Vote totals split into `Main` and `Tie-Breaker` types everywhere they're aggregated (`event_detail`, `live_vote_counts`, `event_analytics`); only `status='Success'` transactions count.
- FR-17: A live vote-count JSON endpoint (`/event/<id>/live-counts/`) exists and is polled every 5 seconds by the front end, but **only when the viewer is the organizer or a staff user** — ordinary voters do not see running counts (`event_detail.html`: "Votes are hidden until the event ends" for non-privileged viewers).

### 4.3 Ticketing
- FR-18: `Ticket` types belong to an event, have price, optional discounted `old_price` (with computed `discount_percentage`), `quantity_available`, optional image, and `is_active` (hide without deleting).
- FR-19: Ticket purchase quantity is capped by `quantity_available` minus already-`Success`ful purchases for that ticket type, checked at purchase time.
- FR-20: Each purchase records `purchase_method` (Web or USSD) and independently tracks `is_checked_in`/`checked_in_at` and `has_voted` (for the tie-breaker mechanic).
- FR-21: A purchased ticket's reference (`TK-XXNNNN`) is the sole credential for verification, retrieval, tie-breaker voting, and check-in scanning.
- FR-22: E-tickets embed a QR code (generated server-side via the `qrcode` library) with structured text fields (event/name/ticket/qty/ref); the client can trigger an emailed copy of the rendered ticket image, gated to real (non-USSD-synthetic) email addresses, rate-limited, and validated for file type/size.
- FR-23: Check-in scanning is idempotent and returns a distinct "already used" response (HTTP 409) including who/when it was previously checked in.
- FR-24: Organizers can export a CSV guest list and a CSV of voting codes; both apply a CSV-macro-injection sanitizer (`sanitize_csv_value`) to any string cell.

### 4.4 USSD
- FR-25: A single `POST /ussd/callback/` endpoint (CSRF-exempt, as required by USSD gateways) implements a `*`-delimited, stateless menu tree with two top-level flows: Vote (Pay-to-Vote by nominee code only) and Buy Ticket.
- FR-26: USSD "payments" are not routed through Paystack or any mobile-money API in this codebase — both the USSD vote and USSD ticket purchase create their respective records with `status='Success'` unconditionally upon confirmation, i.e., payment collection for USSD is simulated/assumed, not verified in code.
- FR-27: USSD buyers/voters are identified only by phone number, stored as a synthetic email address `<phone>@ussd.vote` (no separate phone number field exists on `VoteTransaction`/`TicketPurchase`).
- FR-28: USSD ticket buyers retrieve their e-ticket afterward via the web (`/retrieve-ticket/` or `/tickets/`) using their phone number or the `TK-` reference given in the final USSD message.

### 4.5 Store
- FR-29: `Product`s (with `ProductCategory`) are platform-wide (not organizer/event-scoped), managed only by staff.
- FR-30: Public store page supports category filtering and displays discount percentage when `old_price > price`.
- FR-31: No purchase/checkout flow exists for `Product`s — this is a display/catalogue feature only.

### 4.6 Admin / Moderation
- FR-32: Django Admin is the only interface for approving/unapproving organizers (bulk actions `approve_organizers` / `unapprove_organizers` on the `User` list, each sending a branded HTML email on approval).
- FR-33: Django Admin registers all core models (`Event`, `Category`, `Candidate` with list filters, `VoteTransaction`, `Ticket`, `TicketPurchase`, `VotingCode`, `ProductCategory`, `Product` with list filters) for full CRUD by staff, in addition to the custom organizer-facing views.
- FR-34: `ActivityLog` records key organizer actions (event creation, candidate add/update, category add/update, bulk candidate add, voting-code retrieval) with `user`, `event`, `action` text, and timestamp; surfaced on the dashboard sidebar — staff see the 5 most recent platform-wide entries, organizers see their own 5 most recent.
- FR-35: Login (`login_view`) and Registration (`register_view`) are both IP-rate-limited using Django's cache framework (5 login attempts/min, 3 registration attempts/min).

### 4.7 Notifications
- FR-36: Organizer registration triggers an email to all superusers (best-effort, non-blocking).
- FR-37: Organizer approval triggers a branded HTML email to the organizer.
- FR-38: Voting-code retrieval (Student ID mode) emails the code to the voter-supplied email address and logs the retrieval to `ActivityLog`.
- FR-39: Ticket purchase confirmation can trigger an emailed e-ticket image attachment on demand from the success page (not automatic on payment — it's a client-initiated POST after the QR is rendered), skipped for USSD buyers.
- FR-40: All outbound app email uses `EMAIL_BACKEND = smtp` with `DEFAULT_FROM_EMAIL` from environment; sends are wrapped in `fail_silently=True` / try-except so email failures never break the underlying request.

## 5. Non-Functional Requirements

- **Security**
  - Paystack webhook signature is verified with `hmac.compare_digest` over HMAC-SHA512 of the raw request body against `PAYSTACK_SECRET_KEY`; unsigned/invalid requests get HTTP 400.
  - CSRF protection is applied by default (Django middleware) except on the three externally-driven endpoints that must be exempt: `ussd_callback`, `paystack_webhook`, `send_ticket_email` (each has its own compensating control — USSD gateway trust, webhook signature, and IP rate-limit/validation respectively).
  - `SESSION_COOKIE_SECURE` and `CSRF_COOKIE_SECURE` are enabled whenever `DEBUG=False` (`vote_fund/settings.py`).
  - CSV exports (voting codes, guest lists) are sanitized against CSV/formula injection (`sanitize_csv_value` prepends `'` to values starting with `=`, `+`, `-`, `@`).
  - Login and registration are IP-based rate-limited via cache to slow brute-force/spam.
  - Authorization is enforced ad hoc per view (`request.user == event.organizer or request.user.is_staff`) rather than via Django's permission/group system — every new organizer-only view must remember to add this check manually; there is no row-level permissions framework.
  - Image uploads (`background_image`, `event_image`, candidate/product/ticket images) are size-capped at 2MB via `validate_file_size`; ticket-email image attachments are capped at 5MB and restricted to PNG/JPEG.
- **Availability / Deployment**
  - Runs on Render (`ALLOWED_HOSTS` defaults include `flexyvotes.onrender.com`); media/static asset storage for production is Cloudinary rather than local disk (per `settings.py` comment: "used only for local/DEBUG serving; production storage is Cloudinary").
  - No queue/async worker is present in the code — email sending, QR generation, and Paystack calls all happen synchronously within the request/response cycle.
- **Localization (Ghana / GHS)**
  - `TIME_ZONE = 'Africa/Accra'`, `LANGUAGE_CODE = 'en-us'`.
  - All monetary amounts are implicitly GHS (₵) with no currency field on any model — the app is single-currency by construction, and Paystack amounts are converted `amount * 100` assuming pesewas/kobo-equivalent minor units.
  - Voting-cost convention is hard-coded as "1 GHS = 1 vote" both in the web UI copy and USSD flow.
  - Ticket/voting code UX assumes Ghanaian phone-number conventions for USSD identification (`<phone>@ussd.vote`), with no international phone validation visible in code.
- **Performance expectations for live vote counts**
  - `event_detail`/`live_vote_counts`/`event_analytics` compute vote sums via a single annotated queryset per request (`Coalesce(Sum(...), 0)` filtered by status/vote_type) — O(1) queries per page load regardless of transaction count, but with no caching layer; every poll re-executes the aggregation query against the live `VoteTransaction` table.
  - Live count polling is opt-in and restricted to organizer/admin viewers, capping polling load to a small privileged audience rather than every voter's browser; a 5-second interval is hard-coded client-side with no exponential backoff or visibility-based pausing.
  - No caching (e.g., Redis) is used for vote tallies; at high transaction volume this aggregation could become a bottleneck since there's no materialized/cached counter column.

## 6. Out of Scope / Known Limitations

- **No automated organizer payout.** `Event.get_organizer_payout()` (models.py) only *computes* `total_revenue - (total_revenue * fee%)` as a display figure on the dashboard (`₵{{ event.get_organizer_payout }}`); there is no Paystack transfer/payout API call, no bank/mobile-money account field on `Profile`/`Event`, and no payout-tracking model or admin action anywhere in the codebase. Organizers must be paid out manually, outside the platform.
- **USSD "payments" are not real.** Both USSD voting and USSD ticket purchase mark the resulting `VoteTransaction`/`TicketPurchase` as `status='Success'` immediately upon menu confirmation, without any mobile-money debit/charge API call — there's no Africa's Talking payment integration or MoMo API call in `views.py`. This is a functional gap, not just a documentation gap, if USSD is meant to actually collect money today.
- **No native mobile app.** The entire product is a server-rendered Django/Bootstrap web app plus a USSD callback endpoint; there is no iOS/Android client, and the "scanner" is a browser-based camera feed (`html5-qrcode` JS library) rather than a packaged app.
- **No multi-currency support.** GHS is baked in everywhere (no currency field, `₵`/`GHS` hard-coded in templates and USSD strings); operating outside Ghana would require model and UI changes.
- **No merch checkout.** The Store (`Product`/`ProductCategory`) is browse-only; there is no cart, order, or payment model for products, despite copy on the homepage implying organizers can "sell award plaques directly through your store."
- **No per-organizer store / revenue segregation for merch** — the store is platform-owned and staff-managed only.
- **No formal RBAC.** Authorization is scattered `if` checks per view rather than Django groups/permissions or a policy layer; correctness depends on every view remembering the same manual check.
- **No queueing/async processing.** Payment webhook handling, QR generation, and all emails are synchronous in the request path — a slow SMTP server or Paystack API would directly slow the affected HTTP response.
- **No automated tie-breaker resolution UI** — the `enable_tie_breaker` mechanic grants extra free votes via ticket purchase but there's no dedicated "tie-breaker round" workflow, deadline, or announcement feature; it's simply an always-available parallel voting channel while the flag is on.
- **No vote refunds/cancellations** and no explicit re-run/void of a `VoteTransaction` beyond `Pending → Success`/`Failed` status.
- **USSD voting has no Code-Voting or Student ID path** — USSD only supports Pay-to-Vote by nominee code, not the free/code-based election flow that the web supports.
- **Public vote counts are hidden until event end for anonymous users**, and even the visible ranking percentage (for organizers/admins) is computed on Main votes only, with Tie-Breaker counts shown separately but not blended into the displayed percentage — a design choice worth confirming is intentional if it ever needs to represent a single combined ranking.
