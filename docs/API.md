# API Reference

FlexyVotes has three integration surfaces:

1. **REST API v1** at `/api/v1/`, with interactive docs at `/api/v1/docs` and the schema at
   `/api/v1/openapi.json`.
2. **Inbound callbacks** from Paystack (webhooks) and Africa's Talking (USSD).
3. **Public machine-readable endpoints:** results JSON, the verification bundle and the
   signing key.

The HTML page routes are listed at the end for reference.

## 1. Conventions

| Topic | Rule |
|---|---|
| Base URL | `https://<host>/api/v1` |
| Format | JSON request and response bodies; times in ISO 8601 UTC |
| Pagination | List endpoints take `?page=N` and return `{"items": [...], "count": N}` |
| Correlation | Send `X-Request-ID` or let the server create one; it is echoed back and quoted in errors |
| Idempotency | `POST /elections/{id}/vote` and `POST /elections/{id}/payments` accept `Idempotency-Key` (8–128 characters from `[A-Za-z0-9_-:.]`) |
| Rate limits | Per principal (token, ballot or IP); exceeding a limit returns `429` with `Retry-After` |
| CORS | Off unless the origin is in `API_CORS_ALLOWED_ORIGINS` |

### Idempotency behaviour

| Situation | Result |
|---|---|
| First request with a key | Processed; response stored for 24 h |
| Same key, same body | Stored response replayed, with header `Idempotent-Replay: true` |
| Same key, different body | `422 idempotency_conflict` |
| Same key while the first request is still running | `409 idempotency_in_progress` |

### Errors

Every error uses one envelope:

```json
{"error": {"code": "forbidden", "message": "You do not have permission to perform this action.",
           "details": {}, "correlation_id": "4f0c…"}}
```

| Status | Typical `code` |
|---|---|
| 400 | `invalid_idempotency_key`, `invalid_request` |
| 401 | `unauthenticated`, `invalid_credentials`, `mfa_required`, `locked`, voter sign-in errors |
| 403 | `forbidden`, `not_public` |
| 404 | `not_found`, `not_available` |
| 409 | `not_editable`, `invalid_transition`, `already_voted`, `expired`, `closed`, `unsupported_method`, `idempotency_in_progress` |
| 422 | `validation_error`, `invalid_ballot`, `invalid_dates`, `idempotency_conflict` |
| 429 | `rate_limited` |

## 2. Authentication

| Scheme | Header | Used by |
|---|---|---|
| **API token** | `Authorization: Bearer fv_…` | Staff and organizer integrations |
| **Session** | Session cookie + `X-CSRFToken` on unsafe methods | The web console |
| **Ballot token** | `Authorization: Bearer <ballot_token>` | `GET /ballot`, `POST /vote` only |

Permission checks are the same as in the web console: they depend on the caller's roles for
that organization or election ([SECURITY.md §3](SECURITY.md#3-access-control)).

### `POST /auth/token`: create an API token

No authentication. Limited to 10 per minute.

```json
// request
{"username": "officer1", "password": "…", "otp": "123456", "name": "Results dashboard"}
// 200
{"token": "fv_Q2…", "expires_at": "2027-01-03T10:00:00Z"}
```

- `otp` is required when the user has TOTP enabled.
- Only the SHA-256 of the token is stored. Copy the token when it is returned; it can't be
  shown again.
- Failed attempts count towards account lockout.

| Method & path | Auth | Description |
|---|---|---|
| `DELETE /auth/token` | Token | Revoke the token used for this call (`204`) |
| `GET /auth/me`, `GET /users/me` | Token / session | `{id, username, email, is_platform_admin, organizations}` |

## 3. Organizations

| Method & path | Permission | Description |
|---|---|---|
| `GET /organizations` | any member | Organizations the caller belongs to (page size 50) |
| `GET /organizations/{org_id}` | member | `{id, name, slug, kind, default_timezone, default_currency}` |

## 4. Elections

| Method & path | Auth / permission | Description |
|---|---|---|
| `GET /elections` | optional | Anonymous callers, or `?public=true`: active elections in a public state (Scheduled → Published). Authenticated callers: elections they hold `election.view` on. `?status=OPEN` filters. |
| `POST /elections` | `election.create` | Create a draft election |
| `GET /elections/{id}` | optional | Public if in a public state; otherwise needs `election.view` |
| `PATCH /elections/{id}` | `election.edit` | Change title, description, dates or vote price; obeys the edit policy (`409 not_editable`) |
| `POST /elections/{id}/transitions` | depends on the action | `{"action": "submit", "reason": ""}`; actions in [TRD §2.1](TRD.md#21-lifecycle-electionslifecyclepy) |
| `GET /elections/{id}/positions` | optional | Positions with ballot rules |
| `POST /elections/{id}/positions` | `election.edit` | Add a position |
| `GET /elections/{id}/candidates` | optional | Candidates |
| `POST /elections/{id}/candidates` | `candidate.create` | Add a candidate |
| `GET /elections/{id}/voters` | `voter.view` | Voter roll with masked emails; `?status=VOTED` (page size 100) |
| `POST /elections/{id}/voters` | `voter.import` | Bulk upsert: a list of `{identifier, full_name, email, phone, constituency, attributes}` |
| `GET /elections/{id}/turnout` | `vote.view`, or public after close | Aggregate turnout; the constituency breakdown is shown only with permission |
| `GET /elections/{id}/results` | public per visibility, or `results.view` | Certified results with signature, or the latest unofficial tally for officials |
| `GET /elections/{id}/verification` | optional | The verification bundle (same as `/verify/{id}/bundle.json`) |

### Create an election

```json
// POST /elections
{"title": "SRC General Election 2027", "mode": "INSTITUTIONAL",
 "start_date": "2027-03-10T08:00:00Z", "end_date": "2027-03-10T18:00:00Z",
 "timezone": "Africa/Accra", "currency": "GHS", "organization_id": 3}
// 201
{"id": 42, "title": "SRC General Election 2027", "mode": "Code Voting", "status": "DRAFT",
 "start_date": "…", "end_date": "…", "timezone": "Africa/Accra", "currency": "GHS",
 "organization_id": 3, "results_visibility": "AFTER_PUBLISH", "accepting_votes": false}
```

- `mode` is `INSTITUTIONAL` or `PAID`.
- `vote_price` applies only to `PAID`.
- `end_date` must be after `start_date` (`422 invalid_dates`).
- Plan limits apply: `403` when the organization has hit its active-election limit.

### Add a position

```json
// POST /elections/42/positions
{"name": "Senate", "ballot_type": "MULTIPLE", "min_select": 1, "max_select": 3, "seats": 3,
 "allow_abstain": true}
```

`ballot_type` is one of `SINGLE`, `FPTP`, `MULTIPLE`, `APPROVAL`, `RANKED`, `SCORE`,
`REFERENDUM`. `max_score` (1–100) applies to `SCORE`.

### Import voters

```json
// POST /elections/42/voters
[{"identifier": "UG2023001", "full_name": "Ama Mensah", "email": "ama@st.ug.edu.gh",
  "constituency": "ENG", "attributes": {"level": "300"}}]
// 200
{"created": 1, "updated": 0, "skipped": 0, "codes_issued": 1, "error_count": 0, "errors": []}
```

Access codes are generated but never returned by this endpoint. Send them with the
invitation flow in the console, or export them there with `voter.credentials`.

## 5. Voting through the API (institutional)

The API supports the **access-code** sign-in method, for elections whose `auth_methods`
include `CODE` and that don't require a second factor. Other methods (OTP, SSO, LDAP) use
the web flow at `/e/{id}/vote/` (`409 unsupported_method`).

| Step | Call |
|---|---|
| 1. Sign in | `POST /elections/{id}/ballot/session` with `{"identifier": "UG2023001", "code": "K7QH-…"}`; limited to 15/min |
| 2. Ballot | `GET /elections/{id}/ballot` with `Authorization: Bearer <ballot_token>` |
| 3. Cast | `POST /elections/{id}/vote` with the ballot token and `Idempotency-Key`; limited to 10/min |

```json
// 1 → 200
{"ballot_token": "…", "expires_at": "2027-03-10T09:12:00Z",
 "ballot": [{"id": 7, "name": "President", "ballot_type": "SINGLE", "min_select": 1, "max_select": 1,
             "allow_abstain": true, "candidates": [{"id": 31, "name": "Kofi Asante"}, …]}, …]}

// 3: selections are keyed by position id
{"selections": {
   "7":  31,                        // SINGLE / FPTP
   "8":  [40, 41],                  // MULTIPLE / APPROVAL
   "9":  [52, 51],                  // RANKED: candidate ids, most preferred first
   "10": {"60": 7, "61": 3},        // SCORE: candidate → score
   "11": "YES",                     // REFERENDUM
   "12": null                       // abstain (where allowed)
}}
// 200
{"tracker": "9f2c…64 hex…", "cast_at": "2027-03-10T09:03:41Z", "election": "SRC General Election 2027",
 "positions": 5, "verify_url": "https://vote.example.com/verify/42/?tracker=9f2c…"}
```

- A ballot session lasts 30 minutes, capped at the election's end. Signing in again revokes
  any earlier unused session.
- `409 already_voted` is returned if the voter's ballot is already in. That includes a race
  with another device: exactly one cast succeeds.
- `422 invalid_ballot` comes with a message that names the broken rule.
- The tracker proves inclusion. It doesn't reveal the choices.

## 6. Paid voting

| Method & path | Auth | Description |
|---|---|---|
| `POST /elections/{id}/payments/quote` | none | `{candidate_id, votes \| package_id, discount_code}` returns the price breakdown and limits |
| `POST /elections/{id}/payments` | none | Same fields plus `email`, `phone`, `name`. Returns `201 {reference, status, amount, currency, votes, bonus_votes, authorization_url, votes_credited, held}`. Send `Idempotency-Key`. Limited to 20/min. |
| `GET /payments/{reference}` | none | Payment status. The reference is a random 14-character secret, and the response has no payer details. |
| `GET /payments` | `payment.view` | Payments for elections the caller can see; `?event_id=`, `?status=SUCCESS` |

To pay, send the payer to `authorization_url` (Paystack Checkout). Votes are credited only
after the webhook or a server-side verify confirms the payment. Poll `GET
/payments/{reference}` until `votes_credited` is `true`, or `held` is `true` (under fraud
review).

## 7. Audit

| Method & path | Permission | Description |
|---|---|---|
| `GET /audit` | `audit.view` | Audit events for the caller's organizations and elections; `?election_id=`, `?event_type=PAYMENT` (prefix match); page size 100 |
| `GET /audit/verify` | `audit.view` | `{chain: {ok, checked, first_bad_seq, message}}` for every chain the caller can see |

## 8. Webhooks and callbacks

### Paystack webhook

Paystack can call any of three URLs, which behave identically:
- `POST /api/v1/webhooks/paystack`
- `POST /payments/webhook/` (the one to configure in the Paystack dashboard)
- `POST /webhook/paystack/` (legacy URL, kept for existing configurations)

Processing:

1. `x-paystack-signature` must equal `HMAC-SHA512(raw body, PAYSTACK_SECRET_KEY)`.
   Otherwise the response is `401 {"outcome": "invalid signature"}` and an audit event is
   written.
2. The payload is de-duplicated by SHA-256. A replay returns `200 {"outcome": "duplicate"}`.
3. Events handled:
   - `charge.success` and `charge.failed`, for vote payments, invoice payments and tickets;
   - `refund.processed` and `refund.failed`;
   - `charge.dispute.create`: the payment becomes DISPUTED, its votes are reversed and a
     fraud event is raised;
   - `charge.dispute.resolve`: REVERSED if the dispute is lost. If the merchant wins, the
     payment returns to SUCCESS, but the votes stay reversed until a person reviews it.

   Anything else returns `200 {"outcome": "ignored"}`.
4. Processing errors return `500`, so Paystack retries. Reconciliation also catches anything
   missed.

The browser return URL is `GET /payments/callback/?reference=…`. It always re-verifies the
payment with Paystack server to server and never trusts the query string.

### Africa's Talking USSD: `POST /ussd/callback/`

Form fields `sessionId`, `phoneNumber`, `text`. The callback must carry
`?token=$USSD_CALLBACK_TOKEN` and/or come from `USSD_ALLOWED_IPS`; otherwise it gets 403.
Menu:

```
1  Vote for candidate → nominee code → number of votes → confirm → mobile-money prompt (Paystack charge)
2  Buy event ticket   → event → ticket → quantity → confirm → mobile-money prompt
```

Nothing is credited until Paystack confirms the mobile-money charge.

## 9. Public machine-readable endpoints

| Path | Description |
|---|---|
| `/results/{id}/results.json` | Public results, respecting the visibility setting |
| `/verify/{id}/bundle.json` | Verification bundle: certification payload and signature, public key, config snapshots, trackers, Merkle root |
| `/verify/{id}/?tracker=<hex>` | HTML inclusion check with Merkle proof |
| `/.well-known/flexyvotes-signing-key.json` | `{"algorithm": "Ed25519", "current": {public_key, key_id}, "previous": [...]}` |
| `/.well-known/security.txt` | Security contact (RFC 9116) |
| `/healthz/live`, `/healthz/ready` | Liveness and readiness probes |
| `/metrics` | Prometheus; needs `Authorization: Bearer $METRICS_TOKEN` or a platform-admin session |

Verifying offline:

```bash
curl -s https://vote.example.com/verify/42/bundle.json -o bundle.json
python tools/verify_election.py bundle.json [--tracker <your tracker>] [--trusted-key <key from .well-known>]...
```

## 10. Web routes (HTML)

**Public and voter pages**

| Path | Page |
|---|---|
| `/`, `/event/{id}/` | Home, event page (contestants, live counts, how to vote) |
| `/e/{id}/vote/` → `verify/` → `ballot/` → `review/` → `receipt/`, `signout/` | Institutional voting flow |
| `/e/{id}/register/` | Voter self-registration |
| `/e/{id}/candidates/`, `/e/{id}/candidates/{cid}/` | Candidate profiles |
| `/e/{id}/dispute/` | File a dispute |
| `/results/`, `/results/{id}/`, `/verify/{id}/` | Published results and verification |
| `/e/{id}/pay/{candidate}/` | Paid-vote checkout |
| `/payments/receipt/{ref}/` | Payment receipt |
| `/tickets/`, `/buy-ticket/{id}/`, `/retrieve-ticket/`, `/verify-ticket/` | Ticketing |
| `/store/` | Merchandise store |
| `/contact/` | Support contact form |
| `/accessibility/` | Accessibility settings |

**Accounts**

| Path | Page |
|---|---|
| `/login/`, `/logout/` (POST), `/register/` | Sign in, sign out, register |
| `/password_reset/…`, `/account/password/` | Password reset and change |
| `/account/security/`, `/account/mfa/` | 2FA, passkeys, sessions, devices, API tokens |
| `/auth/sso/{provider}/start/`, `/callback/` | Single sign-on |
| `/portal/…` | Candidate portal |

**Staff console**

| Path | Page |
|---|---|
| `/console/` | Dashboard |
| `/console/elections/{id}/…` | Overview, settings, ballot and preview, voters, eligibility, results, trustees, integrity, audit, monitor |
| `/console/approvals/`, `/console/audit/`, `/console/team/` | Approvals, audit log, team |
| `/console/organizations/…`, `/console/organizers/` | Organizations, organizers |
| `/console/payments/…`, `/console/fraud/…`, `/console/billing/…` | Payments, fraud, billing |
| `/console/support/…`, `/console/notifications/`, `/console/reports/`, `/console/health/` | Support, notifications, reports, system health |

**Organizer pages carried over from the original app**

| Path | Page |
|---|---|
| `/dashboard/`, `/dashboard/create/` | Organizer dashboard, create event |
| `/event/{id}/edit/`, `add-category/`, `add-candidate/`, `bulk-add/` | Event setup |
| `/event/{id}/analytics/`, `scanner/`, `guestlist/` | Analytics, ticket scanner, guest list |

The old code-voting URLs (`/event/{id}/generate-codes/` and similar) redirect to the
matching voter pages in the new election console.

The Django admin lives at `/${ADMIN_URL}`, which defaults to `admin/`.
