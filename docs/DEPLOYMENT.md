# Deployment Guide: Docker on AWS

FlexyVotes ships as one Docker image that runs as four process types: `web`, `worker`,
`beat` and `migrate` (see [ARCHITECTURE.md](ARCHITECTURE.md#1-system-context)). This
guide covers:

- **Option A: ECS Fargate** (recommended for production). ALB, ECS services, RDS
  PostgreSQL, ElastiCache Redis, Secrets Manager, KMS.
- **Option B: one EC2 host with Docker Compose** (staging and small deployments), using
  `docker-compose.prod.yml` with nginx and PgBouncer in front of RDS.

Local development is in the [README](../README.md#quick-start-docker).

## 0. What is in the repository

| File | Purpose |
|---|---|
| `Dockerfile` | `python:3.12-slim`, non-root `appuser`. Compiles translations and collects static files at build time (with a throw-away key). `HEALTHCHECK` on `/healthz/live`. `INSTALL_DEV=true` adds test tools. |
| `docker-entrypoint.sh` | Roles: `web` (migrate if `RUN_MIGRATIONS=true`, seed the admin, run Gunicorn), `worker`, `beat`, `migrate`, or any other command |
| `gunicorn.conf.py` | Binds `0.0.0.0:$PORT`; `gthread` workers; `GUNICORN_WORKERS`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT`, `GUNICORN_MAX_REQUESTS` |
| `docker-compose.yml` | Local full stack: db, redis, web, worker, beat; pgAdmin under `--profile admin` |
| `docker-compose.prod.yml` | Single-host production: nginx (TLS), web, worker, beat, PgBouncer, Redis, and a `migrate` one-off (`--profile release`) |
| `deploy/nginx/nginx.conf`, `trusted_nat.conf` | TLS, HSTS, edge rate limits, `/metrics` restricted to private ranges; NAT exemptions for campus elections |
| `deploy/monitoring/` | Prometheus scrape config and alert rules |
| `deploy/scripts/` | `backup.sh`, `restore.sh`, `restore-test.sh` ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)) |
| `.github/workflows/ci.yml` | Tests on PostgreSQL, Bandit, pip-audit, image build, Trivy, ZAP |

## 1. Prepare secrets and keys (both options)

Generate these once per environment, never reuse them across environments, and store them
in AWS Secrets Manager (or the EC2 host's `.env`, mode `600`).

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"     # SECRET_KEY
python manage.py keys generate-kek                                 # FIELD_ENCRYPTION_KEYS (skip if you use KMS_KEY_ID)
python manage.py keys generate-signing-key                         # SIGNING_PRIVATE_KEY (+ public key to publish)
python manage.py keys generate-blind-index                         # BLIND_INDEX_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"     # METRICS_TOKEN, USSD_CALLBACK_TOKEN
```

- **KMS (recommended).** Create a symmetric KMS key (`alias/flexyvotes-kek`) and set
  `KMS_KEY_ID` to its ARN. Give the task role `kms:Encrypt` and `kms:Decrypt` on that key
  only. The KEK then never leaves KMS.
- **Back up the keys.** Run `python manage.py keys backup --out keys.enc` (it prompts for a
  passphrase, or reads `KEY_BACKUP_PASSPHRASE`). Keep the file offline, separate from
  database backups. Without the KEK, encrypted columns can't be recovered.
- **`SECRET_KEY` is load-bearing.** Access codes, OTPs, ballot sessions and recovery codes
  are HMAC'd with it, so changing it invalidates every live credential. Rotate it only
  between elections ([OPERATIONS.md](OPERATIONS.md#key-management)).

## 2. Build and push the image

```bash
aws ecr create-repository --repository-name flexyvotes --image-scanning-configuration scanOnPush=true
aws ecr get-login-password | docker login --username AWS --password-stdin <acct>.dkr.ecr.<region>.amazonaws.com

TAG=$(git rev-parse --short HEAD)
docker build --build-arg APP_VERSION=$TAG -t <acct>.dkr.ecr.<region>.amazonaws.com/flexyvotes:$TAG .
docker push <acct>.dkr.ecr.<region>.amazonaws.com/flexyvotes:$TAG
```

Always deploy an immutable tag (the git SHA), never `latest`, so rollback is just
redeploying the previous tag.

## 3. Option A: ECS Fargate (production)

### 3.1 Network

- A VPC across 2–3 Availability Zones, with public subnets (ALB, NAT gateways) and
  private subnets (ECS tasks, RDS, ElastiCache).
- **NAT gateway with Elastic IPs.** Outbound calls to Paystack, Africa's Talking, SMTP and
  OIDC leave from these IPs. If your Paystack key has an IP allow-list, add them.
  Otherwise verification and reconciliation fail with *"Your IP address is not allowed to
  make this call"*.
- **Security groups:**

  | Group | Inbound |
  |---|---|
  | ALB | 80 and 443 from anywhere |
  | Tasks | `$PORT` (8000) from the ALB group only |
  | RDS | 5432 from the task group |
  | Redis | 6379 from the task group |

### 3.2 Data stores

**RDS PostgreSQL 16**
- Multi-AZ, storage encrypted with KMS, automated backups for 14–35 days (this enables
  PITR), deletion protection, Performance Insights, and `rds.force_ssl=1`.
- Create two roles, so the application can't drop the append-only triggers:

  ```sql
  CREATE ROLE flexyvotes_owner LOGIN PASSWORD '…';           -- runs migrations, owns the schema
  CREATE DATABASE flexyvotes OWNER flexyvotes_owner;
  \c flexyvotes
  CREATE ROLE flexyvotes_app LOGIN PASSWORD '…';             -- used by web / worker / beat
  GRANT USAGE ON SCHEMA public TO flexyvotes_app;
  ALTER DEFAULT PRIVILEGES FOR ROLE flexyvotes_owner IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO flexyvotes_app;
  ALTER DEFAULT PRIVILEGES FOR ROLE flexyvotes_owner IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO flexyvotes_app;
  ```

  The `migrate` task uses the owner's `DATABASE_URL`, and the services use the app role's.
  The app role isn't the table owner, so it can't drop the triggers.
- For many tasks, put **RDS Proxy** in front, and set `DB_CONN_MAX_AGE=0` and
  `DB_DISABLE_SERVER_SIDE_CURSORS=True`.
- An optional read replica goes in `DATABASE_REPLICA_URL` and is used for reporting reads.

**ElastiCache Redis 7**
- Replication group with Multi-AZ, in-transit and at-rest encryption, and
  `maxmemory-policy noeviction`. Broker data must never be evicted.
- Settings:
  - `REDIS_URL=rediss://…:6379/0` (cache and rate limits);
  - `CELERY_BROKER_URL=rediss://…:6379/1?ssl_cert_reqs=required`.

**S3**
- Bucket `flexyvotes-backups-<env>` with versioning, SSE-KMS, Object Lock (compliance
  mode) and cross-region replication. Used by `backup.sh`.

**Private media**
- Evidence and manifestos live under `PRIVATE_MEDIA_ROOT`. Mount an **EFS** access point
  at `/app/private_media` in the web and worker tasks, so files survive task replacement.
- Public images (candidate photos, flyers) go to Cloudinary (`MEDIA_STORAGE=cloudinary`).

### 3.3 Task definitions and services

Create one task definition family per role, all from the same image tag. Pass secrets
through the `secrets` block (Secrets Manager ARNs), never as plain `environment` values.

| Service | Command | CPU / memory | Count | Notes |
|---|---|---|---|---|
| `flexyvotes-web` | `["web"]` | 1 vCPU / 2 GB | ≥ 2, autoscale on CPU and request count | `RUN_MIGRATIONS=false`; ALB target |
| `flexyvotes-worker` | `["worker"]` | 1 vCPU / 2 GB | ≥ 2, autoscale on queue depth | `CELERY_CONCURRENCY=4` |
| `flexyvotes-beat` | `["beat"]` | 0.25 vCPU / 0.5 GB | **exactly 1** | Deployment: min 0 %, max 100 %, so two never overlap |
| `flexyvotes-migrate` | `["migrate"]` | 0.5 vCPU / 1 GB | one-off `run-task` | Owner `DATABASE_URL` |

Environment for every role:

```
DEBUG=False
ENVIRONMENT=production
ALLOWED_HOSTS=vote.example.com
CSRF_TRUSTED_ORIGINS=https://vote.example.com
SITE_URL=https://vote.example.com
WEBAUTHN_RP_ID=vote.example.com
WEBAUTHN_ORIGIN=https://vote.example.com
PORT=8000
TRUSTED_PROXY_COUNT=1            # the ALB is the only proxy
FORWARDED_ALLOW_IPS=*            # Gunicorn trusts X-Forwarded-Proto from the ALB
SECURE_SSL_REDIRECT=True
SECURE_HSTS_SECONDS=31536000
ENFORCE_STAFF_MFA=True
LOG_FORMAT=json
MEDIA_STORAGE=cloudinary
PRIVATE_MEDIA_ROOT=/app/private_media
DB_CONN_MAX_AGE=0                # with RDS Proxy
DB_DISABLE_SERVER_SIDE_CURSORS=True
APP_VERSION=<git sha>
```

Secrets for every role:
- `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`;
- `KMS_KEY_ID` (or `FIELD_ENCRYPTION_KEYS`), `SIGNING_PRIVATE_KEY`, `BLIND_INDEX_KEY`;
- `PAYSTACK_SECRET_KEY`, `PAYSTACK_PUBLIC_KEY`;
- `AT_API_KEY`, `USSD_CALLBACK_TOKEN`, `EMAIL_HOST_PASSWORD`, `CLOUDINARY_API_SECRET`;
- `METRICS_TOKEN`, `SENTRY_DSN`;
- `DJANGO_SUPERUSER_PASSWORD` (optional).

The full variable reference is in [OPERATIONS.md](OPERATIONS.md#environment-variables).

### 3.4 Load balancer

- **HTTPS listener (443):** ACM certificate, TLS policy `ELBSecurityPolicy-TLS13-1-2-2021-06`.
- **HTTP listener (80):** redirects to 443.
- **Target group:** type `ip`, port 8000.
  - Health check `GET /healthz/ready`, matcher `200`, interval 15 s, healthy threshold 2,
    unhealthy threshold 3.
  - Deregistration delay 30 s, which matches Gunicorn's graceful timeout.
  - Health checks work even though the ALB sends the target IP as `Host`, because
    `HealthCheckMiddleware` answers them before host validation.
- **Idle timeout** 60 s, the same as `GUNICORN_TIMEOUT`.
- **AWS WAF** (recommended): the managed common rule set, the known-bad-inputs set, and a
  rate-based rule. Exempt Paystack's webhook IPs and any campus NAT ranges from the
  rate-based rule.

### 3.5 First deployment

```bash
# 1. Migrate (owner role)
aws ecs run-task --cluster flexyvotes --launch-type FARGATE --task-definition flexyvotes-migrate \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-a,subnet-b],securityGroups=[sg-tasks],assignPublicIp=DISABLED}"
# wait for exit code 0 (CloudWatch log group /ecs/flexyvotes-migrate)

# 2. Start the services
aws ecs update-service --cluster flexyvotes --service flexyvotes-web    --task-definition flexyvotes-web:N --desired-count 2
aws ecs update-service --cluster flexyvotes --service flexyvotes-worker --task-definition flexyvotes-worker:N --desired-count 2
aws ecs update-service --cluster flexyvotes --service flexyvotes-beat   --task-definition flexyvotes-beat:N --desired-count 1

# 3. Check
curl -fsS https://vote.example.com/healthz/ready
aws ecs execute-command --cluster flexyvotes --task <web-task> --interactive \
  --command "python manage.py check --deploy"      # expect no flexyvotes.W00x warnings
```

The first admin comes from `DJANGO_SUPERUSER_*`, which is created or synced when web
starts. Alternatively run `python manage.py createsuperuser` through `execute-command`.
Sign in, then enroll TOTP or a passkey at `/account/security/`. Staff MFA is enforced.

### 3.6 Integrations

| Service | Setting |
|---|---|
| Paystack → Settings → API Keys & Webhooks | Webhook URL `https://vote.example.com/payments/webhook/`; callback URL is set per transaction automatically |
| Africa's Talking → USSD | Callback `https://vote.example.com/ussd/callback/?token=<USSD_CALLBACK_TOKEN>`; optionally `USSD_ALLOWED_IPS` |
| Africa's Talking → SMS | `AT_USERNAME`, `AT_API_KEY`, `AT_SENDER_ID` |
| WhatsApp Cloud API | `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID` |
| Email | Amazon SES SMTP is recommended (`EMAIL_HOST=email-smtp.<region>.amazonaws.com`); verify the domain with DKIM and SPF |
| Google / Microsoft SSO | Redirect URI `https://vote.example.com/auth/sso/google/callback/` (and `/microsoft/`) |
| DNS | Route 53 alias to the ALB; CAA record for Amazon |

### 3.7 Monitoring

- **Logs:** the `awslogs` driver sends them to CloudWatch, one log group per service. The
  logs are JSON, so CloudWatch Logs Insights can query by `request_id`.
- **Metrics:**
  - Amazon Managed Prometheus, or a Prometheus task, scrapes `/metrics` on each web task
    with `Authorization: Bearer $METRICS_TOKEN`. Load `deploy/monitoring/alert_rules.yml`.
  - CloudWatch alarms: ALB 5xx and `TargetResponseTime`, RDS CPU / free storage /
    connections, Redis memory, ECS running count for beat ≠ 1.
- **Errors:** set `SENTRY_DSN`. Traces go to an OTLP collector through
  `OTEL_EXPORTER_OTLP_ENDPOINT`.

## 4. Option B: single EC2 host with Docker Compose

Use this for staging or smaller production, with RDS as the database.

1. **Launch the host.** An Ubuntu 24.04 or Amazon Linux 2023 instance, `t3.large` or
   larger, with Docker and the Compose plugin installed. The security group allows 80 and
   443 from anywhere and 22 only from admin IPs, or use SSM Session Manager instead of SSH.
2. **Get the code and secrets onto the host.**
   ```bash
   git clone <repo> /opt/flexyvotes && cd /opt/flexyvotes
   cp .env.example .env && chmod 600 .env
   # Fill in .env (see §1 and §3.3). DATABASE_URL points to RDS with the owner role.
   # POOLED_DATABASE_URL=postgres://flexyvotes_app:…@pgbouncer:6432/flexyvotes for the services
   ```
3. **TLS certificates** go in `deploy/nginx/certs/` as `fullchain.pem` and `privkey.pem`,
   or set `TLS_CERT_DIR`. To use Let's Encrypt:
   ```bash
   docker run --rm -p 80:80 -v $PWD/deploy/nginx/certs:/etc/letsencrypt certbot/certbot certonly \
     --standalone -d vote.example.com --agree-tos -m ops@example.com
   ```
   Then copy or link `live/vote.example.com/{fullchain,privkey}.pem` into
   `deploy/nginx/certs/`.
4. **Build, migrate and start.**
   ```bash
   export IMAGE=flexyvotes:$(git rev-parse --short HEAD)
   docker build -t $IMAGE .
   docker compose -f docker-compose.prod.yml --profile release run --rm migrate
   docker compose -f docker-compose.prod.yml up -d --scale web=3 --scale worker=2
   curl -fsS https://vote.example.com/healthz/ready
   ```
   Never scale `beat`; exactly one scheduler must run.
5. **Schedule backups and the restore test with cron**
   ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md#3-backups)).

In this layout nginx is the only proxy (`TRUSTED_PROXY_COUNT=1` is set in the compose
file). If you put an ALB in front of nginx, set `TRUSTED_PROXY_COUNT=2`.

## 5. Elections behind a campus NAT

On election day, an institution's voters often share one public IP. Before the election:

1. Ask the institution's IT team for their egress IPs and ranges.
2. Add them to `deploy/nginx/trusted_nat.conf`, one `CIDR 0;` per line, then run
   `nginx -s reload`. On ECS, exempt them in the WAF rate-based rule instead.
3. If needed, raise `VOTER_LOGIN_PER_IP_PER_MIN` (default 120), `VOTER_OTP_PER_IP_PER_5MIN`
   (default 100) and `VOTER_REGISTER_PER_IP_PER_10MIN` (default 30). Per-voter limits are
   not affected, so this doesn't make guessing a code easier.

## 6. Releases and rollback

1. CI must pass: tests on PostgreSQL, Bandit, pip-audit, Trivy and ZAP.
2. Build and push `flexyvotes:<sha>`.
3. **Check the migrations.**
   - Run `python manage.py migrate --plan` against staging first.
   - Migrations must be backwards compatible with the running release: add first, remove
     in a later release.
   - Don't release during an OPEN election unless the change is urgent.
4. Run the `migrate` task with the new image.
5. Update the web, worker and beat services to the new task definition revision. ECS rolls
   them, and the ALB drains old tasks.
6. Smoke-test `/healthz/ready`, sign-in, an election page and `/api/v1/docs`. Watch the
   5xx and latency alarms for 15 minutes.

**Rollback:**
- Redeploy the previous task definition revision.
- If a migration must be undone, run `python manage.py migrate <app> <previous>` with the
  owner role. Only do this when the migration's reverse is safe.
- Never roll back past a data migration during an open election. Fix forward.

## 7. Pre-launch checklist

**Credentials and keys**
- [ ] Credentials ever committed to git are rotated ([SECURITY.md §7](SECURITY.md#7-outstanding-actions-for-the-team)).
- [ ] `SECRET_KEY` is unique to this environment.
- [ ] `KMS_KEY_ID` (or `FIELD_ENCRYPTION_KEYS`), `SIGNING_PRIVATE_KEY` and `BLIND_INDEX_KEY`
  are set. `manage.py keys status` reports no stale data keys.
- [ ] Key backup stored offline, and `keys restore-check` verified.

**Configuration**
- [ ] `manage.py check --deploy` reports no warnings.
- [ ] `DEBUG=False`; `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, `SITE_URL` and
  `WEBAUTHN_*` match the domain.
- [ ] HTTPS works end to end; `SECURE_SSL_REDIRECT=True`; HSTS is on.
- [ ] `TRUSTED_PROXY_COUNT` matches the real number of proxies.
- [ ] `ENFORCE_STAFF_MFA=True`; the first admin has TOTP or a passkey.
- [ ] `METRICS_TOKEN`, `USSD_CALLBACK_TOKEN` and `SECURITY_CONTACT` set;
  `CAPTCHA_PROVIDER` chosen.

**Infrastructure**
- [ ] RDS: private, encrypted, Multi-AZ, PITR on; the app uses a non-owner role.
- [ ] Redis uses `noeviction`; exactly one beat task is running.
- [ ] pgAdmin is not reachable from the internet.
- [ ] Private media is on persistent storage (EFS or a volume).

**Integrations and recovery**
- [ ] Paystack webhook URL set; NAT egress IPs on Paystack's allow-list if it is enabled.
- [ ] Africa's Talking USSD callback includes `?token=`.
- [ ] Backups are scheduled, and a restore test has passed
  ([DISASTER_RECOVERY.md](DISASTER_RECOVERY.md)).
- [ ] Alerts route to a person who is on call.
