# Operations Runbook

This runbook covers day-to-day operation of FlexyVotes: configuration, election-day
procedures, payments, key management, monitoring and incidents. Deployment is in
[DEPLOYMENT.md](DEPLOYMENT.md); backups and recovery are in
[DISASTER_RECOVERY.md](DISASTER_RECOVERY.md).

Commands are written as `python manage.py …`. Run them inside a web container:
- **ECS:** `aws ecs execute-command --cluster flexyvotes --task <id> --interactive --command "…"`
- **Compose:** `docker compose -f docker-compose.prod.yml exec web …`

## Environment variables

Everything is read from the environment, or from `.env` in development. Values marked
**secret** belong in Secrets Manager.

### Core

| Variable | Default | Purpose |
|---|---|---|
| `DEBUG` | `False` | Never `True` in production; also enables the payment simulator |
| `SECRET_KEY` | — (**secret**, required when `DEBUG=False`) | Django signing, plus the HMAC key for access codes, OTPs, ballot tokens and recovery codes. See [Key management](#key-management). |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,0.0.0.0` | Comma-separated hostnames. Health probes bypass this check. |
| `CSRF_TRUSTED_ORIGINS` | — | e.g. `https://vote.example.com` |
| `SITE_URL` | `http://localhost:8000` | Absolute links in emails, callbacks and SSO |
| `PLATFORM_NAME` | `FlexyVotes` | Branding |
| `ADMIN_URL` | `admin/` | Django admin path; move it off the default |
| `PORT` | `8000` | Port Gunicorn binds to inside the container; compose publishes the same port |
| `ENVIRONMENT` | `production` (or `development` when `DEBUG`) | Tag for logs and Sentry |
| `TIME_ZONE`, `LANGUAGE_CODE`, `DEFAULT_CURRENCY` | `Africa/Accra`, `en`, `GHS` | Platform defaults; each election has its own timezone and currency |
| `APP_VERSION` | `dev` | Shown in `/healthz/live` and in logs; set to the git SHA |

### Database, cache, jobs

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite file | `postgres://user:pass@host:5432/db` (**secret**) |
| `DATABASE_SSL_REQUIRE` | `True` | TLS to PostgreSQL; set `False` only for the local compose database |
| `DATABASE_REPLICA_URL` | — | Read replica for reporting queries |
| `DB_CONN_MAX_AGE` | `600` | Persistent connections; `0` behind PgBouncer or RDS Proxy |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | PostgreSQL `statement_timeout` |
| `DB_DISABLE_SERVER_SIDE_CURSORS` | `False` | `True` with transaction pooling |
| `TEST_DATABASE_NAME` | `test_flexyvotes` | Test database name |
| `REDIS_URL` | — (in-process cache) | Cache and rate limits; **required with more than one process** |
| `CELERY_BROKER_URL` | `REDIS_URL` | Without a broker, tasks run inline (eager) |
| `QUEUE_BACKPRESSURE_THRESHOLD` | `50000` | Queue depth at which non-webhook POSTs get 503 |
| `CELERY_CONCURRENCY`, `CELERY_LOG_LEVEL` | `4`, `info` | Worker options (entrypoint) |
| `RUN_MIGRATIONS` | `true` | Web runs `migrate` on start; set `false` when a separate migrate task does it |
| `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT`, `GUNICORN_MAX_REQUESTS` | 2×CPU+1, `4`, `60`, `2000` | Gunicorn tuning |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies Gunicorn trusts for the forwarded scheme |

### Security

| Variable | Default | Purpose |
|---|---|---|
| `SECURE_SSL_REDIRECT` | `False` | Redirect HTTP to HTTPS (health probes exempt) |
| `SECURE_HSTS_SECONDS` | `0` | e.g. `31536000` once HTTPS works |
| `TRUSTED_PROXY_COUNT` | `0` | Number of proxies in front of the app: `1` for ALB or nginx, `2` for ALB + nginx. Decides the client IP used for rate limits and fraud scoring. |
| `SESSION_COOKIE_AGE` | `28800` | Session lifetime in seconds |
| `ENFORCE_STAFF_MFA` | `False` | Console users must enroll TOTP or a passkey |
| `LOGIN_MAX_FAILURES`, `LOGIN_LOCKOUT_SECONDS` | `5`, `900` | Account lockout |
| `VOTER_LOGIN_PER_IP_PER_MIN` | `120` | Voter sign-in attempts per IP per minute (web and API) |
| `VOTER_OTP_PER_IP_PER_5MIN` | `100` | OTP submissions per IP per 5 minutes |
| `VOTER_REGISTER_PER_IP_PER_10MIN` | `30` | Self-registrations per IP per 10 minutes |
| `CSP_REPORT_ONLY` | `False` | Send CSP in report-only mode (for debugging) |
| `CSP_EXTRA_SCRIPT_SRC`, `CSP_EXTRA_CONNECT_SRC` | — | Extra CSP sources |
| `API_CORS_ALLOWED_ORIGINS` | — | Origins allowed to call `/api/` from a browser |
| `OUTBOUND_HTTP_ALLOWED_HOSTS` | Paystack, Google, Microsoft, Meta Graph, Africa's Talking, CAPTCHA hosts | SSRF allow-list for every outbound call |
| `CAPTCHA_PROVIDER` | empty (honeypot only) | `turnstile`, `hcaptcha` or `recaptcha` |
| `CAPTCHA_SITE_KEY`, `CAPTCHA_SECRET_KEY` | — | Provider keys (the secret key is **secret**) |
| `FORM_MIN_FILL_SECONDS` | `2` | Forms submitted faster than this are treated as bots |
| `WEBAUTHN_RP_ID`, `WEBAUTHN_RP_NAME`, `WEBAUTHN_ORIGIN` | `localhost`, platform name, `SITE_URL` | Passkeys; the RP ID must be the site's domain |
| `SECURITY_CONTACT` | `DEFAULT_FROM_EMAIL` | Published in `/.well-known/security.txt` |
| `METRICS_TOKEN` | — (**secret**) | Bearer token for `/metrics` and detailed `/healthz/ready` |

### Keys

| Variable | Default | Purpose |
|---|---|---|
| `FIELD_ENCRYPTION_KEYS` | derived from `SECRET_KEY` (dev only) | `kid:base64key[,kid2:key2…]`; the first is active (**secret**) |
| `KMS_KEY_ID`, `AWS_REGION` | —, `eu-north-1` | Use AWS KMS as the KEK instead (takes precedence) |
| `SIGNING_PRIVATE_KEY` | derived (dev only) | Ed25519 key for signed configs and results (**secret**) |
| `SIGNING_PREVIOUS_PUBLIC_KEYS` | — | Comma-separated public keys of retired signing keys, still trusted for verification |
| `BLIND_INDEX_KEY` | derived (dev only) | HMAC key for email and phone lookups (**secret**) |

### Payments, messaging, SSO, media

| Variable | Default | Purpose |
|---|---|---|
| `PAYSTACK_SECRET_KEY`, `PAYSTACK_PUBLIC_KEY` | — | **secret** / public |
| `PAYSTACK_BASE_URL` | `https://api.paystack.co` | |
| `PAYMENTS_FAKE_GATEWAY` | `DEBUG and no Paystack key` | Simulator; refused when `DEBUG=False` |
| `PAYMENT_ABANDON_AFTER_MINUTES` | `30` | When an unfinished checkout is marked abandoned |
| `REFUND_DUAL_APPROVAL_THRESHOLD` | `500` | Refunds above this amount need a second approver |
| `AT_USERNAME`, `AT_API_KEY`, `AT_SENDER_ID` | `sandbox`, —, — | Africa's Talking SMS (the API key is **secret**) |
| `USSD_CALLBACK_TOKEN`, `USSD_ALLOWED_IPS` | — | USSD callback authentication; set at least one |
| `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` | — | WhatsApp Cloud API |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS` | `smtp.gmail.com`, `587`, `True` | SMTP |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | — | SMTP credentials (**secret**) |
| `DEFAULT_FROM_EMAIL` | `EMAIL_HOST_USER` | Sender address |
| `GOOGLE_OIDC_CLIENT_ID` / `_SECRET` | — | Google SSO |
| `MICROSOFT_OIDC_TENANT` / `_CLIENT_ID` / `_CLIENT_SECRET` | `common`, —, — | Microsoft Entra SSO |
| `MEDIA_STORAGE` | `cloudinary` when configured and not `DEBUG`, else `local` | Public image storage |
| `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET` | — | Cloudinary (the API secret is **secret**) |
| `PRIVATE_MEDIA_ROOT` | `./private_media` | Evidence and manifestos; must be persistent storage |

### Fraud, billing, observability, bootstrap

| Variable | Default | Purpose |
|---|---|---|
| `FRAUD_MONITOR_THRESHOLD`, `FRAUD_CHALLENGE_THRESHOLD`, `FRAUD_HOLD_THRESHOLD` | `31`, `61`, `81` | Risk-score bands |
| `FRAUD_FLAG_PROXIES` | `True` | Score anonymizer and proxy traffic |
| `BILLING_VAT_RATE`, `BILLING_LEVY_RATE`, `BILLING_TRIAL_DAYS` | `15.0`, `6.0`, `14` | Invoice taxes and trial length |
| `LOG_FORMAT` | `text` | `json` in production |
| `DJANGO_LOG_LEVEL` | `INFO` | |
| `SENTRY_DSN`, `OTEL_EXPORTER_OTLP_ENDPOINT` | — | Error tracking and tracing |
| `DJANGO_SUPERUSER_USERNAME`, `_EMAIL`, `_PASSWORD` | — | `seed_admin` creates or syncs this admin on every web start |
| `PGADMIN_DEFAULT_EMAIL`, `PGADMIN_DEFAULT_PASSWORD`, `PGADMIN_PORT` | —, —, `5050` | pgAdmin (compose `admin` profile only) |

## Management commands

| Command | Use |
|---|---|
| `check --deploy` | Django and FlexyVotes deployment checks (`flexyvotes.W001`–`W007`) |
| `seed_admin` | Create or sync the superuser from `DJANGO_SUPERUSER_*` |
| `verify_integrity [--skip-evidence]` | Verify audit chains, signed config snapshots, result certifications (signature, result hash, bulletin root), evidence checksums, and that the configured keys unwrap every data key and decrypt the encrypted columns. Non-zero exit on any failure. |
| `keys status` | KEK provider, data keys (and any not on the active KEK), signing-key source and fingerprint |
| `keys generate-kek` / `generate-signing-key` / `generate-blind-index` | Create new key material |
| `keys rotate` | New data key, then re-encrypt every encrypted column |
| `keys rewrap` | Re-wrap all data keys with the active KEK (after adding a KEK or switching to KMS) |
| `keys reindex` | Recompute blind indexes (after setting or changing `BLIND_INDEX_KEY`) |
| `keys backup --out F` / `keys restore-check --in F` | Passphrase-encrypted key backup, and verifying it |
| `compilemessages` | Rebuild translations after editing `locale/*/django.po` |

## Key management

### Moving a deployment from derived keys to dedicated keys

A deployment that started without dedicated keys has data keys wrapped by the KEK derived
from `SECRET_KEY`. The derived key also signs results and builds the blind indexes. To
move it onto dedicated keys:

1. **Prepare.** Back up the database. Then run `python manage.py keys status`; it shows
   the KEK as `dev` and the signing key as *derived*.
2. **Keep old signatures verifiable.** Copy the current public key, from `keys status` or
   `/.well-known/flexyvotes-signing-key.json`, into `SIGNING_PREVIOUS_PUBLIC_KEYS`.
3. **Set the new keys.** Set `FIELD_ENCRYPTION_KEYS` (or `KMS_KEY_ID`),
   `SIGNING_PRIVATE_KEY` and `BLIND_INDEX_KEY`, then deploy.
   - Existing data stays readable. The derived KEK remains available for unwrapping only.
4. **Migrate the data.** Run `python manage.py keys rewrap`, which moves every data key onto
   the new KEK, and then `python manage.py keys reindex` for the blind indexes.
5. **Verify.** Run `python manage.py keys status` and confirm there are no warnings. Then
   run `python manage.py verify_integrity`.

### Routine rotation

| What | How | Effect |
|---|---|---|
| Data key (yearly) | `keys rotate` | New encryptions use the new key; old values are re-encrypted |
| KEK (yearly, or on suspected exposure) | Prepend a new `kid:key` to `FIELD_ENCRYPTION_KEYS`, deploy, run `keys rewrap`, then remove the old entry | No downtime |
| KMS key | Enable KMS automatic rotation; KMS keeps old versions | Nothing to do |
| Signing key | New `SIGNING_PRIVATE_KEY`; add the old public key to `SIGNING_PREVIOUS_PUBLIC_KEYS` | Old certifications still verify |
| Blind-index key | Change `BLIND_INDEX_KEY`, then `keys reindex` immediately | Lookups fail until the reindex finishes; do it outside elections |
| `SECRET_KEY` | **Only between elections.** Change it, then reissue voter credentials for any upcoming election | Invalidates sessions, access codes, OTPs, ballot sessions and recovery codes |
| Paystack and other API keys | Rotate in the provider dashboard, update the secret, redeploy | — |

## Election-day runbook

**Before opening (T−1 day)**

1. Check the election overview: status SCHEDULED, configuration snapshot signed, ballot
   preview correct, voter count correct.
2. Make sure invitations have been sent: use **Send invitations to all** in the voter list,
   or **Send invitation** on individual voters.
3. Check the network and service limits:
   - campus NAT IPs are added to `trusted_nat.conf` or the WAF ([DEPLOYMENT.md §5](DEPLOYMENT.md#5-elections-behind-a-campus-nat));
   - the SMS and email providers have enough credit and sending quota;
   - web and worker capacity is scaled up.
4. Confirm on-call staff and their roles. The **Election Officer** helps voters, and the
   **Reviewer** handles dual approvals.

**During voting**

- **Monitoring.**
  - `/console/elections/<id>/monitor/` shows turnout, the voting rate and errors.
  - Watch the `VoteSubmissionFailures`, `VoteLatencyHigh` and `QueueBacklog` alerts.
- **Voter support.**

  | Problem | Action |
  |---|---|
  | Lost code | The voter uses "Lost your access code?". It goes only to the email on the roll. |
  | Wrong email on the roll | An officer edits the voter, then resets the credential. Bulk resets need dual approval. |
  | "Already voted" but the voter says they didn't | Don't reset anything. Open an incident, record a dispute, and preserve evidence. The ballot can't be removed (append-only), by design. |
  | Many "too many attempts" errors from one site | Shared NAT; see election-day step 3. |

- **Problems with the election itself.**
  - Pause voting from the overview if there is a systemic problem, and record an incident.
  - Extending the end time needs dual approval (`EXTEND_VOTING`).

**After closing**

1. **Tally.** Start the tally as a Results Officer. Check the invalid-ballot count, and
   any reported ties.
2. **Certify.** A different Results Officer certifies.
3. **Publish.** Then confirm `/verify/<id>/` shows "all passed".
4. **Disputes.** Resolve them before archiving. Apply a legal hold if a challenge is
   expected.

## Payments runbook

| Situation | Action |
|---|---|
| Fan says they paid but no votes | `/console/payments/` → search by reference or email (blind index). If PENDING, use "Re-verify with Paystack". If held, see the next row. |
| Payment held by fraud | `/console/fraud/` → review the signals → approve (credits votes) or reject (refund). |
| Reconciliation discrepancy | `/console/payments/reconciliation/` → each item says what differs. "Missing success" items are fixed automatically; resolve the rest and add a note. |
| Reconciliation run FAILED "IP address is not allowed" | Add the egress IPs (NAT gateway or EC2) to the Paystack key's IP allow-list. |
| Refund | Payment detail → request refund. Above `REFUND_DUAL_APPROVAL_THRESHOLD` a second Finance Officer approves it in `/console/approvals/`. |
| Chargeback | Automatic: votes reversed, fraud alert raised. Review the payer and blocklist them if needed. |
| Webhook failures alert | `/console/payments/` webhook list. Paystack retries on 500, and reconciliation covers any gaps. |

## Monitoring

| Signal | Where |
|---|---|
| Liveness / readiness | `/healthz/live`, `/healthz/ready` (detailed checks with `Authorization: Bearer $METRICS_TOKEN`) |
| Metrics | `/metrics`, scraped with the token. Series: `fv_http_requests_total`, `fv_http_request_duration_seconds`, `fv_db_query_duration_seconds`, `fv_vote_submissions_total{outcome}`, `fv_vote_latency_seconds`, `fv_votes_total`, `fv_open_elections`, `fv_payments_total{status}`, `fv_webhooks_total{outcome}`, `fv_failed_webhooks_last_hour`, `fv_reconciliation_discrepancies_total`, `fv_fraud_alerts_total`, `fv_open_fraud_alerts`, `fv_logins_total`, `fv_rate_limited_total`, `fv_notifications_total`, `fv_queue_depth` |
| Alerts | `deploy/monitoring/alert_rules.yml`: `AppDown`, `HighServerErrorRate`, `VoteSubmissionFailures`, `VoteLatencyHigh`, `PaymentSuccessRateLow`, `WebhookFailures`, `QueueBacklog`, `DatabaseSlow`, `FraudAlertSpike`, `RateLimitingSpike` |
| System health page | `/console/health/`: database, cache and broker checks, pending migrations, queue depths, business signals (payments and success rate in the last hour, webhook failures, ballots per hour, open fraud alerts, notifications queued and failed), integrations, circuit breakers, audit-chain verification |
| Logs | JSON on stdout. Search by `request_id`, which equals the `X-Request-ID` response header and the error envelope's `correlation_id`. |

## Incident response

1. **Declare.** Record an incident in `/console/elections/<id>/integrity/` (or at
   platform level), with its severity. The record is audited and timestamped.
2. **Contain.**
   - Pause affected elections.
   - Blocklist abusive IPs, devices or cards in `/console/fraud/blocklist/`.
   - Revoke sessions (`/account/security/` for your own account; deactivate other users
     in the admin).
   - Revoke API tokens.
3. **Preserve.** Upload evidence; each file gets a SHA-256 when it is uploaded. Apply a
   legal hold. Run `python manage.py verify_integrity` and keep the output.
4. **Audit-chain break alert** (CRITICAL log + admin email). Treat it as tampering:
   - Find the first bad sequence number with `GET /api/v1/audit/verify`.
   - Compare against the latest backup.
   - Check PostgreSQL logs for DDL, trigger drops or superuser sessions.
5. **Credential exposure.** Rotate the affected keys ([Key management](#key-management)).
   If `SECRET_KEY` leaked during an election, pause the election, rotate the key, reissue
   credentials, and record the incident.
6. **Communicate and review.** Notify the organization's admins. Write a post-incident
   review within 5 working days.

## Routine tasks

| Task | How |
|---|---|
| Approve a new organizer | `/console/organizers/` → approve. Creates their personal organization and Organization Admin role. |
| Add staff to an organization | `/console/organizations/<id>/team/` → grant a role, scoped to the organization or one election |
| Support tickets | `/console/support/` → reply. Internal notes aren't sent to the requester. |
| Database access | pgAdmin (compose `--profile admin`) behind VPN or IP allow-list only, or `psql` through a bastion or SSM. Use a read-only role for browsing. |
| Translations | Edit `locale/fr/LC_MESSAGES/django.po`, run `compilemessages`, and commit both `.po` and `.mo` |
| Monthly | Restore test ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md#4-restore-testing)), `pip-audit`, review the fraud blocklist and expired API tokens |
| Quarterly | Access review (role assignments per organization), key backup check (`keys restore-check`) |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `400 Bad Request` on every page | The `Host` isn't in `ALLOWED_HOSTS` |
| CSRF failures on login over plain HTTP | Cookies are `Secure` when `DEBUG=False`. Use HTTPS, or `DEBUG=True` locally. |
| Static files 404 | The image was built without `collectstatic` (see the Dockerfile), or `DEBUG=False` without WhiteNoise in the middleware |
| Encrypted fields show errors after a config change | A KEK was removed while data keys still used it. Restore the old entry in `FIELD_ENCRYPTION_KEYS`, then run `keys rewrap`. |
| Voters at one site get 429 | Shared NAT; see [DEPLOYMENT.md §5](DEPLOYMENT.md#5-elections-behind-a-campus-nat) |
| Scheduled elections don't open | `beat` isn't running (or two are). Check `celery -A vote_fund inspect ping` and the beat logs. |
| Emails not delivered | `/console/notifications/` shows failures and their errors; check SMTP credentials and quotas |
