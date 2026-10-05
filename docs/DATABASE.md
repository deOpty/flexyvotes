# Database Schema

- **Engine:** PostgreSQL 16 in production and CI; SQLite works for local development.
- **Primary keys:** `BigAutoField` by default. Tables whose ids must not be guessable or
  ordered use a UUID: `Ballot`, `VoteAuthorization`, `Payment`, `Refund`, `OTPChallenge`
  and `Notification`.
- **Model source:** each app's `models.py`. This page summarizes the tables and documents
  the constraints that the integrity guarantees depend on.

## 1. Entity overview

```
Organization ─┬─< RoleAssignment >── Role          User ─┬─ UserSecurity, WebAuthnCredential, UserSession,
              │        └─ (optional) Event               │   KnownDevice, ApiToken
              ├─< Constituency (tree, materialized path) └─ Profile (legacy organizer flag)
              ├── Subscription ── Plan ;  Invoice ─< InvoiceLine ;  UsageRecord ;  FeatureOverride
              └─< Event ─┬─< Category (position) ─< Candidate ─< CandidateDocument
                         │                 └─< VoteTransaction ── Payment (1:1, paid votes)
                         ├─< Voter ─< VoteAuthorization            (identity side)
                         ├─< Ballot                                (anonymous side - no FK to Voter)
                         ├── ElectionKey ;  TrusteeShare ;  ElectionConfigSnapshot
                         ├─< ElectionResult ─< ResultCertification ;  Recount
                         ├─< ApprovalRequest ;  Dispute / Incident ─< CaseNote, EvidenceItem
                         ├─< VotePackage ;  DiscountCode ;  Payment ─< PaymentEvent, Refund
                         └─< Ticket ─< TicketPurchase

AuditChainHead ─< AuditEvent (hash chain per organization)     DataKey (wrapped encryption keys)
WebhookEvent ; ReconciliationRun ─< ReconciliationItem ; FraudEvent ; BlocklistEntry ; Notification
IdempotencyRecord ; OTPChallenge ; SupportTicket ─< SupportMessage ; ProductCategory ─< Product ─< ProductImage
```

## 2. Tables by app

### core

| Table | Key columns | Notes |
|---|---|---|
| `core_organization` | `name`, `slug` (unique), `kind`, default timezone, currency and language, `is_personal`, `sso_config`, `ldap_config`, `directory_config` | Tenant. The three configs are **encrypted JSON**. |
| `core_role` | `code` (unique), `permissions` (JSON list), `is_system` | Synced from `core/rbac.py` after every `migrate` |
| `core_roleassignment` | `user`, `role`, `organization` (nullable), `event` (nullable), `granted_by` | Scope: platform (both null), organization, or one election. Unique on `(user, role, organization, event)`. |
| `core_auditchainhead` | `chain` (unique), `seq`, `last_hash` | Locked on every append |
| `core_auditevent` | `chain`, `seq`, `event_type`, actor, organization and election ids, target, `summary`, `changes`, `metadata`, `result`, `ip_address`, `correlation_id`, `prev_hash`, `hash` (unique) | **Append-only.** Unique `(chain, seq)`. Indexed on `(election_id, created_at)`, `(organization_id, created_at)` and `(actor_id, created_at)`. |
| `core_idempotencyrecord` | `scope`, `key`, `request_hash`, `state`, `response_status`, `response_body`, `expires_at` | Unique `(scope, key)`; purged daily |
| `core_datakey` | `purpose`, `provider` (`local` or `aws-kms`), `kek_id`, `wrapped_key`, `is_active` | Data keys, stored wrapped by a KEK; never plaintext |
| `core_usersecurity` | `user` (1:1), `totp_secret` (**encrypted**), `recovery_codes` (HMACs), `failed_login_count`, `locked_until`, `mfa_enforced` | |
| `core_webauthncredential` | `credential_id` (unique), `public_key`, `sign_count`, `transports` | Passkeys |
| `core_usersession` | `session_key` (unique), IP, user agent, `device_hash`, `last_seen_at`, `revoked` | |
| `core_knowndevice` | `user`, `device_hash` | Unique `(user, device_hash)` |
| `core_apitoken` | `prefix`, `token_hash` (unique, SHA-256), `expires_at`, `revoked_at` | |
| `core_otpchallenge` (UUID) | `purpose`, `subject_type` / `subject_id`, `channel`, `code_hash`, `attempts` / `max_attempts`, `expires_at`, `consumed_at` | |
| `core_supportticket`, `core_supportmessage` | reference, category, status, priority, assignee; message body, `is_internal` | |

### voting

| Table | Key columns | Notes |
|---|---|---|
| `voting_event` | See the list below | The election |
| `voting_category` | `ballot_type`, `min_select`, `max_select`, `allow_abstain`, `seats`, `max_score`, `referendum_threshold`, `constituency`, `display_order`, `vote_price`, `max_votes_per_voter` | A **position** on the ballot |
| `voting_candidate` | `category`, `event`, `name`, `bio`, `manifesto`, `affiliation`, `nominee_code` (unique), `image`, `status`, `email`, `user` (portal account) | |
| `voting_votetransaction` | `candidate`, `payment` (**1:1, unique**), `number_of_votes`, `status` (`Success` / `Reversed`), `vote_type` | Paid-vote ledger. Indexed on `(candidate, status)`. |
| `voting_ticket`, `voting_ticketpurchase` | price, quantity; `paystack_reference` (unique), `status`, `is_checked_in` | Ticketing |
| `voting_product*` | | Store |
| `voting_profile` | `is_approved_organizer` | Legacy organizer approval |

`voting_event` columns:
- **Ownership and mode:** `organization`, `organizer`, `voting_mode` (`Pay to Vote` /
  `Code Voting`).
- **Lifecycle:** `status` (indexed), `opened_at`, `closed_at`, `certified_at`,
  `published_at`, `archived_at`.
- **Schedule and locale:** `start_date`, `end_date`, `timezone`, `currency`.
- **Results:** `results_visibility`.
- **Voter sign-in:** `auth_methods` (JSON), `require_second_factor`,
  `allow_self_registration`, `registration_email_domains`.
- **Integrity controls:** `dual_approval_required`, the freeze flags (`config_frozen`,
  `ballot_frozen`, `candidates_frozen`, `voter_list_frozen`), `legal_hold`.
- **Secrecy and keys:** `record_constituency_on_ballot`, `min_anonymity_set`,
  `key_custody`, `trustee_threshold`.
- **Paid-vote limits and pricing:** per-voter, per-transaction and spend limits,
  `payment_channels`, `allowed_countries`, `vote_price`, `platform_fee_percentage`.

### elections

| Table | Key columns | Notes |
|---|---|---|
| `elections_constituency` | `organization`, `parent`, `name`, `code`, `kind`, `path` (indexed) | Tree via materialized path. Unique `(organization, code)`. |
| `elections_voter` | See the list below | **Identity side** |
| `elections_eligibilityrule` | `election`, `position` (nullable), `kind`, `attribute`, `values`, `constituency` | Election-wide or per position |
| `elections_voteauthorization` (UUID) | `election`, `voter`, `token_hash` (unique), `status` (ISSUED / CONSUMED / REVOKED / EXPIRED), `auth_method`, `ballot_style`, `expires_at`, `consumed_at` | **One CONSUMED row per voter** (partial unique) |
| `elections_ballot` (UUID) | `election`, `ciphertext`, `tracker` (unique), `style_hash`, `constituency_id` (nullable, plain integer) | **Anonymous side, append-only.** No voter FK, no token, no timestamp. |
| `elections_electionkey` | `election` (1:1), `public_key`, `fingerprint`, `custody`, `wrapped_private_key` (**encrypted**; empty under trustee custody), `threshold`, `shares` | |
| `elections_trusteeshare` | `election`, `trustee`, `index`, `share_hash`, `pending_share` / `submitted_share` (**encrypted**) | Unique `(election, trustee)` and `(election, index)` |
| `elections_electionconfigsnapshot` | `election`, `version`, `config`, `config_hash`, `signature`, `public_key`, `key_id` | Unique `(election, version)` |
| `elections_electionresult` | `kind`, `status`, `data`, `result_hash`, `bulletin_root`, counts, `tallied_by`, `reviewed_by` | |
| `elections_resultcertification` | `result`, `payload`, `payload_hash`, `signature`, `public_key`, `key_id`, `certified_by`, `revoked_at` | |
| `elections_approvalrequest` | `action`, `payload`, `status`, `requested_by`, `decided_by`, `expires_at`, `result` | Dual approval |
| `elections_recount` | `kind`, `result`, `compared_to`, `matches`, `differences` | |
| `elections_dispute` | `reference` (unique), `filer_email` (**encrypted**), `category`, `status`, `resolution` | |
| `elections_incident`, `elections_casenote` | `severity`, `status`; notes on disputes and incidents | |
| `elections_evidenceitem` | `file` (private storage), `sha256`, `size`, `uploaded_by` | **Append-only** |
| `elections_candidatedocument` | `file` (private), `sha256` | Manifestos |

`elections_voter` columns:
- `identifier` (student or staff ID).
- `full_name`, `email` and `phone`, all **encrypted**. `email_index` and `phone_index`
  are blind indexes.
- `constituency`, `attributes` (JSON) and `status`.
- `credential_hash` (HMAC of the access code) and `credential_ciphertext` (**encrypted**).
  The ciphertext is kept so officials can resend the code, and wiped when the voter votes.
- `user`, `sso_subject_index`, `voted_at`.
- Constraints: unique `(election, identifier)` and unique `(election, credential_hash)`,
  each a partial unique that applies only when the value is not null.

### payments

| Table | Key columns | Notes |
|---|---|---|
| `payments_votepackage` | `votes`, `bonus_votes`, `price`, promotion window, `max_per_payer` | Vote bundles |
| `payments_discountcode` | `code` (unique), `kind`, `value`, redemption limits, window | |
| `payments_payment` (UUID) | See the list below | |
| `payments_paymentevent` | `payment`, `from_status`, `to_status`, `source`, `message`, `data`, `actor` | **Append-only** status history |
| `payments_webhookevent` | `payload_hash` (unique), `event_type`, `reference`, `status`, `attempts` | Webhook dedupe and inbox |
| `payments_refund` (UUID) | `amount`, `status`, `reverse_votes`, `requested_by`, `approved_by` | |
| `payments_reconciliationrun` / `…item` | window, counts, status; per-payment discrepancy, `resolution` | |

`payments_payment` columns:
- **Identity of the payment:** `reference` (unique), `idempotency_key` (partial unique),
  `purpose` (VOTE / INVOICE).
- **What was bought:** event, candidate, package, discount, invoice; `votes`,
  `bonus_votes`.
- **Money:** `unit_price`, `gross_amount`, `discount_amount`, `amount`, `currency`.
- **Payer:** `payer_email` and `payer_phone` (**encrypted**, with blind indexes),
  `payer_name`.
- **State:** `status` (indexed), `gateway_status`.
- **Card details:** signature, country, last 4 digits.
- **Risk:** `risk_score`, `risk_decision`, `held`.
- **Credit:** `votes_credited`, `credited_at`, `refunded_amount`.
- Indexes: `(event, status)` and `(status, created_at)`.

### fraud, notifications, billing

| Table | Key columns | Notes |
|---|---|---|
| `fraud_fraudevent` | `kind`, `decision`, `score`, `signals`, links to event, payment and candidate, `status`, `reviewed_by` | Alerts and decisions |
| `fraud_blocklistentry` | `kind` (IP / CIDR / device / email / domain / card / phone / anonymizer), `value`, `organization` (empty = platform-wide), `expires_at` | Indexed on `(kind, value)`. Emails and phones are stored as blind indexes. An organization's entries only apply to its own events. |
| `notifications_notification` (UUID) | `channel`, `template`, `recipient` (**encrypted**), `context` (**encrypted**, wiped after sending), `status`, `attempts`, `dedupe_key` (unique) | Outbox |
| `billing_plan` | `code` (unique), prices, `included_voters`, `limits`, `features` | Seeded after `migrate` |
| `billing_subscription` | `organization` (1:1), `plan`, `status`, `billing_cycle`, trial and period dates, `coupon` | |
| `billing_featureflag`, `billing_featureoverride` | `key` (unique); `(flag, organization)` unique | |
| `billing_coupon` | `code` (unique), `kind`, `value`, `duration_months`, redemption limits | |
| `billing_usagerecord` | `organization`, `metric`, `quantity`, `invoice` | Indexed on `(organization, metric, recorded_at)` |
| `billing_invoice`, `billing_invoiceline` | `number` (unique), subtotal, discount, `levy_rate` / `levy`, `vat_rate` / `vat`, `total`, `status` | |

## 3. Integrity constraints worth knowing

| Guarantee | Enforced by |
|---|---|
| A voter casts at most one ballot | `one_consumed_authorization_per_voter` (partial unique) + row lock in `cast_ballot()` + `Voter.status` |
| A ballot token is single-use | `token_hash` unique + `SELECT … FOR UPDATE` + `CONSUMED` state |
| A payment credits votes at most once | `VoteTransaction.payment` OneToOne (unique) + row lock in `apply_gateway_result()` |
| A retried payment request creates one payment | `uniq_payment_idempotency_key` + `IdempotencyRecord` unique `(scope, key)` |
| A webhook is processed once | `WebhookEvent.payload_hash` unique |
| The audit chain can't fork | `uniq_audit_chain_seq` + `AuditChainHead` row lock |
| Voter IDs and credentials are unique per election | `uniq_voter_identifier`, `uniq_voter_credential` |
| Trustee shares can't be duplicated | `uniq_trustee_per_election`, `uniq_trustee_index` |

## 4. Append-only tables

`core/migrations/0002_append_only_triggers.py` installs a PL/pgSQL trigger
(`flexyvotes_append_only`) on these tables, firing `BEFORE UPDATE OR DELETE`:

- `core_auditevent`
- `elections_ballot`
- `elections_evidenceitem`
- `payments_paymentevent`

Any update or delete raises `insufficient_privilege`, even from pgAdmin or a raw SQL
session. The ORM querysets for these models refuse `update()` and `delete()` as well.
`core.tests.test_crypto_audit` checks that the triggers are installed and that they work.

To remove data legitimately (for example, a court-ordered erasure), a DBA must drop the
trigger. That is DDL, which needs elevated rights and appears in the PostgreSQL logs. It
also breaks the audit chain verification, which is intentional.

## 5. Encryption at rest

Fields declared as `EncryptedTextField` or `EncryptedJSONField` (`core/fields.py`) are
stored as `fv1$<data_key_id>$<base64>` ciphertext. The format is AES-256-GCM with
associated data `app.model.field`, so a value copied into another column won't decrypt.

| Encrypted column | Lookup by |
|---|---|
| `Voter.full_name`, `email`, `phone`, `credential_ciphertext` | `email_index`, `phone_index` (HMAC blind index) |
| `Payment.payer_email`, `payer_phone` | `payer_email_index`, `payer_phone_index` |
| `Dispute.filer_email` | |
| `Notification.recipient`, `context` | |
| `UserSecurity.totp_secret` | |
| `ElectionKey.wrapped_private_key`, `TrusteeShare.pending_share`, `submitted_share` | |
| `Organization.sso_config`, `ldap_config`, `directory_config` | |

Key rotation:
- `manage.py keys rotate` creates a new data key and re-encrypts every encrypted column.
- `manage.py keys rewrap` re-wraps data keys under the current KEK.

See [OPERATIONS.md](OPERATIONS.md#key-management).

## 6. Migrations

| App | Migrations |
|---|---|
| `voting` | `0001`–`0035`: original app. `0036_platform_election_fields`: new columns. `0037_migrate_legacy_data`: data move (see below). `0038_remove_legacy_models`: drops `ActivityLog` and `VotingCode`. |
| `core` | `0001_initial`, `0002_append_only_triggers` (PostgreSQL only; does nothing on SQLite) |
| `elections`, `payments`, `notifications` | `0001_initial` |
| `fraud` | `0001`, `0002`, `0003_blocklistentry_organization` |
| `billing` | `0001`, `0002` |

What `0037` does:
- Gives every legacy organizer a personal organization and the Organization Admin role.
- Maps legacy event states to the new lifecycle.
- Turns legacy `VotingCode` rows into `Voter` rows. Codes are kept as HMACs, so existing
  codes still work, and emails are encrypted.
- Writes audit events for the move.

This was tested against seeded pre-upgrade rows, and against an existing development
database that was at `0035`. For production, take a backup first and run the restore test
([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)) before migrating.

Rules for new migrations:
- Never edit an applied migration. Add a new one.
- Run `python manage.py makemigrations --check` before committing; CI enforces it.
- Large tables: add a column as nullable, backfill it in a separate migration, then add the
  constraint. In production, migrations run as a separate one-off task before new app
  containers start ([DEPLOYMENT.md](DEPLOYMENT.md)).

## 7. Retention

| Data | Retention |
|---|---|
| Idempotency records | 24 h (purged daily) |
| OTP challenges | Deleted 1 day after expiry |
| User sessions | Deleted after 90 days idle |
| Unused ballot authorizations | Marked EXPIRED daily |
| Notification context | Wiped as soon as the message is sent |
| Voter access-code ciphertext | Wiped when the voter votes |
| Audit events, ballots, payment events, evidence | Permanent (append-only) |
