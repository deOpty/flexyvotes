# Operations Runbook

Day-2 operations reference for running FlexyVotes in production. Pairs with
`DEPLOYMENT.md` (how it gets deployed) and `SECURITY.md` (what to watch for).

## Environment variables reference

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | Yes | Unique per environment. Never reuse across dev/staging/prod. |
| `DEBUG` | No (default `False`) | Must be `False` in production. |
| `ALLOWED_HOSTS` | Yes in prod | Comma-separated. Must match the domain(s) users hit. |
| `CSRF_TRUSTED_ORIGINS` | Yes in prod | Comma-separated, must include the scheme (`https://...`). |
| `SITE_URL` | Yes | Used to build PayStack callback URLs and links inside notification emails. Must be the real public HTTPS URL. |
| `PORT` | No (default `8000`) | Port gunicorn/the container listens on. Change if `8000` conflicts with another service; `docker-compose.yml` and the ECS task definition must use the same value for port mapping/health checks. |
| `DATABASE_URL` | Yes in prod | Postgres connection string. Omit to fall back to local SQLite (dev only). |
| `DATABASE_SSL_REQUIRE` | No (default `True`) | Only set to `False` for a local, non-TLS Postgres (e.g. docker-compose). Never set `False` in production. |
| `SECURE_SSL_REDIRECT` | No (default `False`) | Set `True` once HTTPS termination in front of the app is confirmed working. |
| `SECURE_HSTS_SECONDS` | No (default `0`) | Set a real value (e.g. `31536000`) only after `SECURE_SSL_REDIRECT` is confirmed safe. |
| `PAYSTACK_SECRET_KEY` | Yes | Live key in production, test key in dev/staging. |
| `AT_USERNAME` / `AT_API_KEY` | Yes for USSD | Africa's Talking sandbox or production credentials. |
| `EMAIL_HOST_USER` / `EMAIL_HOST_PASSWORD` | Yes | Gmail account + app password (see `DEPLOYMENT.md` note on migrating to SES). |
| `CLOUDINARY_CLOUD_NAME` / `CLOUDINARY_API_KEY` / `CLOUDINARY_API_SECRET` | Yes | All media storage in every environment. |
| `DJANGO_LOG_LEVEL` | No (default `INFO`) | Root logger level. |
| `DJANGO_SUPERUSER_USERNAME` / `_EMAIL` / `_PASSWORD` | No | If username+password are both set, the container entrypoint auto-creates this admin superuser on start (idempotent — skipped if it already exists). Leave unset to create one manually instead. |

## Routine tasks

### Approve a new organizer
Organizers self-register but cannot create events until approved:
1. Log in to `/admin/` as a superuser.
2. Go to Users, select the pending account(s).
3. Use the **"Approve selected as Organizers"** admin action. This sets `Profile.is_approved_organizer=True` and emails the user.

### Rotate a credential
1. Generate the new value with the provider (PayStack/Africa's Talking/Cloudinary/Gmail).
2. Update the corresponding secret in AWS Secrets Manager (see `DEPLOYMENT.md` §4).
3. Force a new ECS deployment so tasks pick up the new secret value: `aws ecs update-service ... --force-new-deployment`.
4. Revoke/delete the old credential at the provider once the new deployment is healthy.

### Create a superuser
The simplest option is to set `DJANGO_SUPERUSER_USERNAME`/`_EMAIL`/`_PASSWORD`
in the environment/Secrets Manager — the entrypoint's `manage.py seed_admin`
step creates it automatically on the next deploy/restart if it doesn't
already exist. To create one manually instead:
```bash
aws ecs run-task --cluster flexyvotes-cluster --launch-type FARGATE \
  --task-definition flexyvotes \
  --overrides '{"containerOverrides":[{"name":"flexyvotes","command":["python","manage.py","createsuperuser"]}]}' \
  ...
```
(See `DEPLOYMENT.md` §5 for the full command with network configuration.)

### Apply a schema change
Migrations run automatically on every container start via
`docker-entrypoint.sh`. For a migration with real data-volume risk (adding a
`NOT NULL` column to a large table, a data migration, etc.), review the
generated SQL (`python manage.py sqlmigrate voting <migration_number>`)
before releasing, and consider running it as a one-off task ahead of the
rolling deploy rather than relying on the automatic path.

## Backups

- RDS: enable automated backups (`--backup-retention-period 7` or higher) and take a manual snapshot before any risky migration or major release.
- Media: stored in Cloudinary, not on the app's infrastructure — no separate backup needed for uploaded images, but note Cloudinary's own retention/plan limits.
- `db.sqlite3` is **local-dev only** — never used in production; no backup relevant there.

## Monitoring & alerting

- CloudWatch Logs: all app/gunicorn output (see `DEPLOYMENT.md` §8).
- Recommended alarms: ALB `HTTPCode_Target_5XX_Count`, ECS service CPU/memory, RDS `FreeStorageSpace` and `CPUUtilization`.
- Watch for repeated `django.request` ERROR-level log lines — `settings.py`'s `LOGGING` config routes these to the console/CloudWatch at `ERROR` level specifically so they're easy to filter.

## Known operational limitations to plan around

- **Rate limiting is per-process** (`LocMemCache`): login/registration/email-resend throttling resets per container and isn't shared across multiple ECS tasks. With `desired count >= 2`, an attacker effectively gets N× the intended attempt budget by hitting different tasks. Move to a shared cache (e.g. ElastiCache Redis, via `django-redis`) before/while scaling beyond a single task if this matters for your risk tolerance.
- **Synchronous email sending**: organizer-approval emails, ticket emails, and voting-code retrieval emails are sent inline during the request (not via a task queue). A slow/unavailable SMTP server will slow down or fail the triggering request. Consider a task queue (Celery + SQS/Redis) if email volume grows.
- **USSD "payments" are not verified** (see `SECURITY.md` #14) — until real Africa's Talking mobile-money confirmation is wired in, USSD votes/tickets are effectively free. Factor this into any reporting/reconciliation process.
- **`collectstatic` requires `--upload-unhashed-files`**: `django-cloudinary-storage`'s bundled `collectstatic` override is a no-op without this flag (see `DEPLOYMENT.md` §0). If you ever run it outside the `Dockerfile` (e.g. debugging a non-container deploy), forgetting the flag silently produces "0 static files copied" and every static asset 404s — always include it.

## Incident response quick reference

- **Suspected credential leak**: rotate immediately (see "Rotate a credential" above), then review `SECURITY.md` #2 for the git-history remediation steps.
- **Payment/webhook issues**: check CloudWatch Logs for `paystack_webhook` entries; verify the PayStack dashboard shows the webhook delivering successfully with `200` responses; confirm `PAYSTACK_SECRET_KEY` matches the key configured in the PayStack dashboard for signature verification.
- **USSD not responding**: verify the Africa's Talking dashboard's configured callback URL matches `https://<domain>/ussd/callback/` and that the ALB/security groups allow inbound traffic from Africa's Talking's IP ranges.
