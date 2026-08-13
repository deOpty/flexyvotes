# Deployment Guide — Docker on AWS

This guide covers containerizing FlexyVotes with Docker and deploying it to
AWS. It assumes the fixes and hardening documented in `SECURITY.md` have been
applied (they are, in this repository's current state) and that you have
**rotated every credential** that was ever committed to `.env` before doing
anything else — see `SECURITY.md` finding #2. Do not deploy with the old
credentials.

Two deployment paths are covered:
- **Recommended: ECS Fargate** — fully managed containers, no servers to patch, scales cleanly. Use this for production.
- **Alternative: single EC2 instance with Docker Compose** — cheaper/simpler for a low-traffic or staging environment; more ops burden.

---

## 0. Files added for containerization

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage-ready image build: installs deps, runs `collectstatic`, drops to a non-root user, defines a healthcheck, runs `gunicorn` |
| `.dockerignore` | Keeps secrets, docs, VCS metadata, and local artifacts out of the build context/image |
| `docker-entrypoint.sh` | Runs `manage.py migrate` on container start, then execs the given command (gunicorn) |
| `docker-compose.yml` | Local Postgres + web container for testing the containerized app before pushing to AWS |

**Verify the build locally before touching AWS:**

```bash
cp .env.example .env   # fill in real (rotated) values, or test values for Paystack/AT sandbox
docker compose up --build
# App: http://localhost:8000/  (or http://localhost:${PORT}/ if you changed PORT in .env)
```

`docker compose` runs Postgres with `DATABASE_SSL_REQUIRE=False` (local
Postgres has no TLS configured) — production must NOT set this to `False`;
leave `DATABASE_SSL_REQUIRE` unset (defaults to `True`) so RDS connections
require TLS.

This has been verified end-to-end against a real containerized Postgres:
image builds, all 29 migrations apply cleanly, the app serves `/`, `/login/`,
`/admin/`, and static assets correctly, and `python manage.py test` (13
tests) passes inside the running container.

**Port is configurable.** Set `PORT` in `.env` (default `8000`) if 8000 is
already used by another service on your machine — `docker-compose.yml` maps
the same value on the host and inside the container, so changing it in one
place is enough. The `Procfile` (for non-Docker PaaS deploys) and the
Dockerfile's `gunicorn` command both bind to `0.0.0.0:${PORT:-8000}`.

**Admin account.** Set `DJANGO_SUPERUSER_USERNAME`/`_EMAIL`/`_PASSWORD` in
`.env` and the entrypoint will automatically create that Django superuser on
container start (idempotent — skipped if it already exists). Leave them
unset to create your admin manually instead (see §5).

> **Gotcha found while verifying the build:** `django-cloudinary-storage`
> registers its own `collectstatic` management command (it takes precedence
> over Django's built-in one purely because `cloudinary_storage` is listed
> in `INSTALLED_APPS`). That command's file-copy step is a no-op unless
> `STATICFILES_STORAGE` is Cloudinary's own `StaticCloudinaryStorage`, or the
> `--upload-unhashed-files` flag is passed — this app uses Whitenoise for
> static files (Cloudinary is only used for media), so without the flag,
> `collectstatic` silently reports "0 static files copied" and the admin
> site (and any other static asset) 404s in production. The `Dockerfile`
> already passes this flag; if you ever run `collectstatic` by hand (e.g.
> outside Docker, for a non-container deploy), always include
> `--upload-unhashed-files`.

---

## 1. Prerequisites

- AWS account with permissions for: ECR, ECS, RDS, Secrets Manager, IAM, ALB/VPC, CloudWatch.
- AWS CLI v2 configured (`aws configure`) with a profile that has the above permissions.
- Docker installed locally.
- A domain name you control (for HTTPS via ACM) — optional but strongly recommended.
- Rotated production credentials for: PayStack (live secret key), Africa's Talking, Gmail (or a transactional email provider — see note below), Cloudinary.

> **Email note:** the app currently sends mail via Gmail SMTP with an app
> password (`EMAIL_HOST_USER`/`EMAIL_HOST_PASSWORD`). This works but Gmail
> rate-limits and can flag automated sending. For a production deployment at
> any real volume, consider swapping to Amazon SES or another transactional
> provider — this only requires changing `EMAIL_HOST`/`EMAIL_PORT`/credentials
> in `settings.py`/environment, not application logic.

---

## 2. Build and push the image to Amazon ECR

```bash
# One-time: create the repository
aws ecr create-repository --repository-name flexyvotes --region <your-region>

# Authenticate Docker to ECR
aws ecr get-login-password --region <your-region> \
  | docker login --username AWS --password-stdin <account-id>.dkr.ecr.<your-region>.amazonaws.com

# Build and tag
docker build -t flexyvotes:latest .
docker tag flexyvotes:latest <account-id>.dkr.ecr.<your-region>.amazonaws.com/flexyvotes:latest

# Push
docker push <account-id>.dkr.ecr.<your-region>.amazonaws.com/flexyvotes:latest
```

Re-run the `build`/`tag`/`push` steps for every release. Tag with a version
or git SHA instead of only `latest` once you have a CI pipeline, so you can
roll back to a specific image.

---

## 3. Provision the database — Amazon RDS (PostgreSQL)

```bash
aws rds create-db-instance \
  --db-instance-identifier flexyvotes-prod \
  --db-instance-class db.t4g.micro \
  --engine postgres \
  --engine-version 16 \
  --master-username flexyvotes \
  --master-user-password '<strong-generated-password>' \
  --allocated-storage 20 \
  --vpc-security-group-ids <sg-id> \
  --db-subnet-group-name <your-db-subnet-group> \
  --backup-retention-period 7 \
  --no-publicly-accessible
```

- Keep the instance in a private subnet (`--no-publicly-accessible`); only the
  ECS service's security group should be allowed to connect on port 5432.
- `db.t4g.micro` is a reasonable starting size; scale up based on real load.
- Once available, build the connection string:
  `postgres://flexyvotes:<password>@<rds-endpoint>:5432/flexyvotes`
- This becomes the `DATABASE_URL` secret (step 4). Leave `DATABASE_SSL_REQUIRE`
  unset in production — RDS supports and should require TLS.

---

## 4. Store secrets in AWS Secrets Manager

Never put real secrets in the ECS task definition as plain environment
variables. Store each one in Secrets Manager and reference it from the task
definition instead.

```bash
aws secretsmanager create-secret --name flexyvotes/SECRET_KEY \
  --secret-string "$(python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())')"

aws secretsmanager create-secret --name flexyvotes/DATABASE_URL \
  --secret-string "postgres://flexyvotes:<password>@<rds-endpoint>:5432/flexyvotes"

aws secretsmanager create-secret --name flexyvotes/PAYSTACK_SECRET_KEY --secret-string "<rotated live key>"
aws secretsmanager create-secret --name flexyvotes/AT_API_KEY --secret-string "<rotated key>"
aws secretsmanager create-secret --name flexyvotes/EMAIL_HOST_PASSWORD --secret-string "<rotated app password>"
aws secretsmanager create-secret --name flexyvotes/CLOUDINARY_API_SECRET --secret-string "<rotated secret>"
aws secretsmanager create-secret --name flexyvotes/DJANGO_SUPERUSER_PASSWORD --secret-string "<a strong password>"
```

Non-secret configuration (`DEBUG=False`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`,
`SITE_URL`, `PORT`, `AT_USERNAME`, `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`,
`EMAIL_HOST_USER`, `DJANGO_SUPERUSER_USERNAME`, `DJANGO_SUPERUSER_EMAIL`) can
go directly in the task definition's `environment` block rather than Secrets
Manager. `PORT` only needs to match the container port mapping/target group
port (§6) — 8000 is fine unless you have a specific reason to change it in
this environment.

---

## 5. Run migrations against RDS (first deploy only, then per-release if needed)

The container's entrypoint (`docker-entrypoint.sh`) already runs
`manage.py migrate --noinput` on every container start, so migrations apply
automatically as part of a normal deploy/rollout — no separate manual step is
required for routine releases (all 29 existing migrations were verified to
apply cleanly against a fresh Postgres database during this review).

**Admin account:** the same entrypoint also runs `manage.py seed_admin` on
every start, which creates a superuser from `DJANGO_SUPERUSER_USERNAME`/
`_EMAIL`/`_PASSWORD` if one with that username doesn't already exist — set
those three env vars/secrets (§4) and the first deploy will have a working
admin login with no extra step. If you'd rather not manage the password as
infrastructure config, leave them unset and create the superuser manually
via a one-off task instead:

```bash
aws ecs run-task \
  --cluster flexyvotes-cluster \
  --launch-type FARGATE \
  --task-definition flexyvotes \
  --overrides '{"containerOverrides":[{"name":"flexyvotes","command":["python","manage.py","createsuperuser","--noinput"]}]}' \
  --network-configuration "awsvpcConfiguration={subnets=[<subnet-id>],securityGroups=[<sg-id>],assignPublicIp=DISABLED}"
```

(Set `DJANGO_SUPERUSER_USERNAME`/`DJANGO_SUPERUSER_EMAIL`/`DJANGO_SUPERUSER_PASSWORD` env vars for `--noinput` to work, or drop `--noinput` and don't pass those.)

---

## 6. ECS Fargate service behind an Application Load Balancer

Every reference to port `8000` below is really the `PORT` env var's value
(default `8000`) — if you set a non-default `PORT` in the task definition,
use that same value for the container port mapping, the target group port,
and the health check port.

High-level topology:

```
Internet -> ALB (HTTPS:443, ACM cert) -> Target Group -> ECS Fargate tasks (port 8000)
                                                              |
                                                              v
                                                     RDS PostgreSQL (private subnet)
```

Steps:

1. **VPC**: use an existing VPC or create one with at least two public subnets (for the ALB) and two private subnets (for ECS tasks + RDS).
2. **Security groups**: ALB SG allows inbound 443/80 from the internet. ECS task SG allows inbound 8000 only from the ALB SG. RDS SG allows inbound 5432 only from the ECS task SG.
3. **ALB + Target Group**: create an ALB in the public subnets, a target group with target type `ip`, health check path `/login/` (a lightweight page that returns `200`), port `8000`.
4. **ACM certificate**: request/validate a certificate for your domain, attach it to the ALB's HTTPS:443 listener. Redirect HTTP:80 → HTTPS:443 at the listener level.
5. **ECS cluster**: `aws ecs create-cluster --cluster-name flexyvotes-cluster`.
6. **Task definition**: Fargate, the image from ECR, port mapping `8000`, environment variables for non-secret config, `secrets` block referencing the Secrets Manager ARNs from step 4, log configuration pointed at a CloudWatch Logs group (`awslogs` driver). CPU/memory: `512`/`1024` is a reasonable starting point.
7. **ECS service**: desired count `2` (for availability across AZs), attached to the ALB target group, deployed into the private subnets with `assignPublicIp=DISABLED` (outbound internet access — for PayStack/Cloudinary/Africa's Talking/SMTP calls — via a NAT Gateway in the VPC).
8. **DNS**: point your domain's A/ALIAS record (Route 53 or your DNS provider) at the ALB.

Once the ALB terminates HTTPS and forwards to the app, set these in the task
definition's environment and redeploy:

```
SECURE_SSL_REDIRECT=True
SECURE_HSTS_SECONDS=31536000
ALLOWED_HOSTS=your-domain.example.com
CSRF_TRUSTED_ORIGINS=https://your-domain.example.com
SITE_URL=https://your-domain.example.com
```

`settings.py` already reads `SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')`,
which matches how ALB forwards the original protocol — no extra Django config needed for that part.

---

## 7. Static & media files in production

- **Static files** (`STATIC_URL`/`STATIC_ROOT`) are baked into the image at
  build time via `collectstatic` (in the `Dockerfile`) and served directly by
  Whitenoise from inside the container — no separate S3/CloudFront step is
  required to get a working deployment. For higher-traffic production use,
  consider fronting the ALB with CloudFront to offload static asset serving
  and add edge caching.
- **Media files** (user-uploaded images) are stored in Cloudinary in all
  environments (`DEFAULT_FILE_STORAGE`), not on the container's local disk —
  this is already correct for a multi-instance/ephemeral-container deployment
  and requires no additional AWS storage (no EFS/S3 needed for uploads).

---

## 8. Logging & monitoring

- The ECS task definition's `awslogs` log driver sends stdout/stderr (where
  `settings.py`'s `LOGGING` config and gunicorn both write) straight to
  CloudWatch Logs — no extra app-side logging config is needed.
- Set a CloudWatch Alarm on the ALB's `HTTPCode_Target_5XX_Count` and on ECS
  service CPU/memory utilization.
- The Docker image's `HEALTHCHECK` (`GET /login/`) is for local
  `docker run`/`docker compose` visibility; ECS uses the ALB target group
  health check (same path) for actual routing/replacement decisions.

---

## 9. Release process for subsequent deploys

1. `docker build -t flexyvotes:<tag> .`
2. `docker push` to ECR with the new tag.
3. Update the ECS task definition's image to the new tag (new task definition revision).
4. `aws ecs update-service --cluster flexyvotes-cluster --service flexyvotes-service --task-definition flexyvotes:<new-revision> --force-new-deployment`
5. ECS performs a rolling deployment; the entrypoint script runs `migrate` on
   each new task before gunicorn starts, so schema changes land automatically.
   For destructive/long-running migrations, review them manually before
   release rather than relying on this automatic path.

---

## 10. Alternative: single EC2 instance with Docker Compose

For a lower-cost staging environment or very low traffic, you can run
`docker-compose.yml` directly on a single EC2 instance instead of ECS:

1. Launch an EC2 instance (e.g. `t3.small`, Amazon Linux 2023) with Docker and the Compose plugin installed.
2. Copy the repository (or just `Dockerfile`, `docker-compose.yml`, `.env`) to the instance.
3. Put real secrets in a `.env` file on the instance (not in git) — same variables as `.env.example`.
4. `docker compose up -d --build`.
5. Put an ALB (or just an Elastic IP + your DNS) in front of it, or use `certbot`/a reverse-proxy container for TLS directly on the instance.

This is simpler to operate but has no automatic failover, no rolling
deploys, and mixes the database and app on infrastructure you patch
yourself — treat it as a stepping stone, not the long-term production setup.

---

## 11. Pre-launch checklist

- [ ] All credentials rotated (see `SECURITY.md` #2) — none of the values that were ever in the committed `.env` are still in use.
- [ ] `SECRET_KEY` is a fresh, unique value for this environment.
- [ ] `DEBUG=False`.
- [ ] `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` set to the real production domain(s).
- [ ] `SECURE_SSL_REDIRECT=True` and `SECURE_HSTS_SECONDS` set once HTTPS via the ALB is confirmed working end-to-end.
- [ ] RDS is not publicly accessible; only the app's security group can reach it.
- [ ] PayStack webhook URL configured in the PayStack dashboard to point at `https://<your-domain>/webhook/paystack/`, using the **rotated live** secret key.
- [ ] Africa's Talking USSD callback URL configured to point at `https://<your-domain>/ussd/callback/`.
- [ ] `python manage.py check --deploy` run against production settings shows no unresolved warnings.
- [ ] `python manage.py test` passes (verified: 13/13 pass against a real containerized Postgres).
- [ ] A superuser account exists — either via `DJANGO_SUPERUSER_*` env vars (auto-seeded on first start) or manually via `createsuperuser` — to approve the first organizers.
- [ ] Static assets actually load (`/static/admin/css/base.css` returns `200`, not `404`) — confirms `collectstatic` ran with `--upload-unhashed-files` (see the gotcha note in §0).
- [ ] `PORT` (if changed from `8000`) is consistent across the container's env var, the ECS container port mapping, the target group port, and the health check port.
