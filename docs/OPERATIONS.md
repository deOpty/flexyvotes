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
| `PGADMIN_DEFAULT_EMAIL` / `PGADMIN_DEFAULT_PASSWORD` | Yes, if using the `pgadmin` compose service | pgAdmin's own login (unrelated to Django/Postgres credentials). Use a strong, unique value — never expose this service's port to the public internet (see `DEPLOYMENT.md`). |
| `PGADMIN_PORT` | No (default `5050`) | Host port pgAdmin is published on via `docker-compose.yml`. |

## Routine tasks

### Approve a new organizer
Organizers self-register but cannot create events until approved:
1. Log in to `/admin/` as a superuser.
2. Go to Users, select the pending account(s).
3. Use the **"Approve selected as Organizers"** admin action. This sets `Profile.is_approved_organizer=True` and emails the user.

### Access the database directly (pgAdmin)
For anything the Django admin doesn't cover (ad-hoc queries, inspecting raw
table data, checking indexes):
1. `docker compose up -d pgadmin` (starts alongside `db`/`web` if not already running).
2. Browse to `http://<host>:${PGADMIN_PORT:-5050}/` and log in with `PGADMIN_DEFAULT_EMAIL`/`PGADMIN_DEFAULT_PASSWORD`.
3. Add a server: host `db`, port `5432`, and the `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB` values from `docker-compose.yml`'s `db` service.
4. Only do this over a network path restricted to admins (SSH tunnel, VPN, or a security-group rule) — see the security warning in `DEPLOYMENT.md`. Do not leave `PGADMIN_PORT` open to the public internet.

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
- **Uploaded image 404s at `/media/...`** (root-caused and fixed during this review — keeping the notes here in case it resurfaces after a settings change): this Django version (`Django==6.0.7`) does **not** derive `default_storage`/`staticfiles_storage` from the legacy `DEFAULT_FILE_STORAGE`/`STATICFILES_STORAGE` settings at all — it only reads the modern `STORAGES` dict. With only the legacy settings defined, `default_storage` silently fell back to Django's built-in `FileSystemStorage`, so every upload was actually written to local disk (hence a real `/media/...` URL, and hence it vanishing on container restart) instead of going to Cloudinary. `vote_fund/settings.py` now defines `STORAGES` (which Django actually reads) alongside the legacy names (which `django-cloudinary-storage`'s own `collectstatic` override still reads directly via `settings.STATICFILES_STORAGE` — removing them entirely causes an `AttributeError` during `collectstatic`, so keep both in sync if either ever changes). To confirm it's working:
  1. `docker compose exec web python manage.py shell -c "from django.core.files.storage import default_storage; print(default_storage.__class__)"` — must print `cloudinary_storage.storage.MediaCloudinaryStorage`, not `FileSystemStorage`.
  2. `docker compose exec web python manage.py shell -c "from django.contrib.staticfiles.storage import staticfiles_storage; print(staticfiles_storage.__class__)"` — must print Whitenoise's `CompressedManifestStaticFilesStorage`.
  3. If either prints the wrong class after a settings change, check that both the legacy setting and the corresponding key in `STORAGES` still agree.
  4. Rows created while this bug was active have their `event_image`/etc. pointing at local paths — Cloudinary won't retroactively have them; re-upload through the app/admin to move them over.
  5. Also confirm `DEBUG=False` in production — `DEBUG=True` is what makes Django serve `/media/` locally at all (`vote_fund/urls.py`) and is what rendered the verbose debug 404 page (which itself leaks internals and shouldn't be shown to real users).
  `docker-compose.yml`'s `media_data` volume (mounted at `/app/media`) still protects whatever legacy local files exist from being wiped by a restart, independent of this fix.
