# Project Tree

An annotated layout of the repository. Generated and vendored folders are left out:
`.venv/`, `staticfiles/`, `media/`, `private_media/` and `backups/`.

```
flexyvotes/
├── manage.py
├── requirements.txt            Pinned runtime dependencies (Django 6.0.8, django-ninja, celery, cryptography, …)
├── requirements-dev.txt        + bandit, pip-audit, polib, responses
├── pyproject.toml              Bandit configuration
├── Dockerfile                  python:3.12-slim, non-root, compilemessages + collectstatic, HEALTHCHECK
├── docker-entrypoint.sh        Roles: web | worker | beat | migrate | <command>
├── gunicorn.conf.py            Gunicorn settings (PORT, workers, threads, timeouts, Prometheus cleanup)
├── docker-compose.yml          Local stack: db, redis, web, worker, beat, pgadmin (profile "admin")
├── docker-compose.prod.yml     Single-host production: nginx, web, worker, beat, pgbouncer, redis, migrate (profile "release")
├── .env.example                Every environment variable, grouped and commented
├── Procfile, runtime.txt       Legacy PaaS process definition and Python version
├── Notes.md                    Running log of requests to the team
│
├── vote_fund/                  Django project
│   ├── settings.py             All configuration from the environment; test profile; logging; CSP; Celery beat schedule
│   ├── urls.py                 Root URLs: admin, /api/v1/, i18n, legacy webhook + USSD, app URLs, error handlers
│   ├── celery.py               Celery app (autodiscovers tasks)
│   └── wsgi.py, asgi.py
│
├── core/                       Platform services shared by every app
│   ├── crypto.py               AES-GCM envelope encryption, KEK providers (local / KMS), blind indexes, Ed25519,
│   │                           ECIES ballot sealing, Shamir, Merkle trees, passphrase backups
│   ├── fields.py               EncryptedTextField / EncryptedJSONField
│   ├── models.py               Organization, Role, RoleAssignment, AuditEvent + chain head, IdempotencyRecord, DataKey,
│   │                           UserSecurity, WebAuthnCredential, UserSession, KnownDevice, ApiToken, OTPChallenge, Support*
│   ├── rbac.py                 Permissions, role definitions, has_perm / check_perm, scoping helpers, decorators
│   ├── audit.py                Hash-chained audit log: record, verify_chain, verify_all
│   ├── auth.py                 Lockout, login steps, TOTP + recovery codes, passkeys, API tokens
│   ├── otp.py, sso.py, ldap_auth.py   One-time codes; OIDC (PKCE, JWKS); LDAP bind
│   ├── middleware.py           Health probes, correlation id, metrics, backpressure, API CORS, session security,
│   │                           preferences, security headers / CSP
│   ├── ratelimit.py, idempotency.py, captcha.py, http.py (SSRF-safe client, circuit breaker), utils.py
│   ├── tenancy.py              Personal organizations, current organization, grant / revoke roles
│   ├── storage.py              Private storage, SHA-256, upload magic-byte validation
│   ├── metrics.py, observability.py, checks.py (flexyvotes.W00x), db_router.py, signals.py, tasks.py
│   ├── views.py                Health, metrics, preferences, accessibility, signing key, security.txt, error pages
│   ├── views_account.py        Security page, MFA, passkeys, SSO start / callback
│   ├── views_console.py        Staff console: dashboard, approvals, audit, team, organizations, organizers, health,
│   │                           support, notifications, reports
│   ├── management/commands/    keys (generate / rotate / rewrap / backup / reindex / status), verify_integrity
│   ├── migrations/             0001_initial, 0002_append_only_triggers (PostgreSQL)
│   ├── templatetags/fv.py      money, number, pct, in_tz, can, status_badge, bot_protection, …
│   ├── static/core/            platform.css, app.js (no inline scripts; CSP-safe)
│   ├── testing.py              FlexyTestRunner (clears the cache per test)
│   └── tests/                  factories.py, test_crypto_audit, test_rbac_security, test_auth_sso, test_i18n
│
├── voting/                     Events, positions, candidates, paid-vote ledger, tickets, store, public pages
│   ├── models.py               Event (lifecycle, auth methods, freezes, limits), Category, Candidate, VoteTransaction,
│   │                           Ticket, TicketPurchase, Product*, Profile
│   ├── views.py                Home, event page, organizer tools, tickets and scanner, store, contact, login / register,
│   │                           USSD, legacy redirects
│   ├── urls.py, admin.py, tests.py
│   ├── management/commands/seed_admin.py
│   ├── migrations/             0001–0035 original; 0036 fields, 0037 legacy data migration, 0038 drop legacy models
│   └── static/voting/          theme.css, site.js
│
├── elections/                  Institutional election engine
│   ├── models.py               Constituency, Voter, EligibilityRule, VoteAuthorization, ElectionKey, TrusteeShare,
│   │                           Ballot, ElectionConfigSnapshot, ElectionResult, ResultCertification, ApprovalRequest,
│   │                           Recount, Dispute, Incident, CaseNote, EvidenceItem, CandidateDocument
│   ├── lifecycle.py            State machine, guards, edit policy, auto open / close
│   ├── ballot.py               Ballot definitions, validation, rules text, configuration problems
│   ├── tally.py                Plurality, approval, score, referendum, IRV / STV (pure functions)
│   ├── casting.py              issue_authorization, cast_ballot (the secret-ballot core)
│   ├── results.py              Tally, certify, recount, public results, verification bundle, Merkle proofs
│   ├── integrity.py            Snapshots, freezes, dual approval, disputes, incidents, evidence, legal hold
│   ├── eligibility.py, keys.py, voters.py (import / codes / invitations / turnout), voter_auth.py, exports.py, tasks.py
│   ├── views_voter.py          /e/<id>/vote/… voting flow, self-registration
│   ├── views_public.py         Candidates, results, verification, disputes
│   ├── views_console.py        Election console (settings, ballot, voters, results, trustees, integrity, monitor)
│   ├── views_portal.py         Candidate portal
│   ├── urls.py                 namespace "elections" + portal_patterns
│   └── tests/                  test_ballot, test_tally, test_secret_ballot, test_web_flows, test_integrity_voters,
│                               test_concurrency (PostgreSQL)
│
├── payments/                   Paystack integration
│   ├── paystack.py             API client (initialize, verify, list_transactions, refund, charge_mobile_money),
│   │                           webhook signatures, Ghana MoMo provider detection, development simulator
│   ├── service.py              Quotes, initiation, apply_gateway_result, state machine, webhooks, refunds, chargebacks,
│   │                           reconciliation, revenue
│   ├── models.py               VotePackage, DiscountCode, Payment, PaymentEvent, WebhookEvent, Refund, Reconciliation*
│   ├── views.py, urls.py       Checkout, callback, receipt, webhook, simulator, finance console
│   └── tasks.py, signals.py, admin.py, tests/
│
├── fraud/                      engine.py (signals and scoring), service.py, models.py (FraudEvent, BlocklistEntry),
│                               views.py (alerts, blocklist), tasks.py (anomaly scans), data/disposable_domains.txt, tests/
├── notifications/              models.py (Notification outbox), service.py (notify), channels.py (email / SMS /
│                               WhatsApp / in-app), tasks.py (deliver, retry), tests/
├── billing/                    models.py (Plan, Subscription, Coupon, UsageRecord, Invoice, FeatureFlag), service.py
│                               (plan catalog, limits, invoicing), views.py, signals.py (seed plans), tasks.py, tests/
├── api/                        api.py (django-ninja REST API v1: auth, organizations, elections, ballot, payments,
│                               webhooks, audit), tests/
│
├── templates/                  Server-rendered UI (125 templates)
│   ├── voting/                 base.html (site layout), public event pages, organizer pages, tickets, store
│   ├── vote/                   Voter flow: base, _steps, start (sign-in), otp, ballot (also the console preview),
│   │                           review, receipt, register
│   ├── public/                 Candidates, results, results index, verification, dispute
│   ├── console/                Staff console layout and pages (incl. console/elections/*)
│   ├── payments/, fraud/, billing/   Checkout, receipt, finance, fraud and billing console pages
│   ├── portal/                 Candidate portal
│   ├── account/                Security, MFA, password change
│   ├── core/                   Error pages, accessibility page
│   └── notifications/          Email (HTML + text) and SMS templates
├── locale/fr/LC_MESSAGES/      French translations (django.po, compiled django.mo)
│
├── deploy/
│   ├── nginx/                  nginx.conf (TLS, HSTS, edge limits), trusted_nat.conf (NAT exemptions), certs/ (ignored)
│   ├── monitoring/             prometheus.yml, alert_rules.yml
│   └── scripts/                backup.sh, restore.sh, restore-test.sh
├── tools/verify_election.py    Offline verifier for published elections (standard library + cryptography)
├── .github/workflows/ci.yml    Tests (PostgreSQL + Redis), Bandit, pip-audit, image build, Trivy, ZAP
├── .zap/rules.tsv              ZAP baseline rule tuning
│
└── docs/                       README index lives at ../README.md
    ├── FEATURES.md             Original feature requirements
    ├── IMPLEMENTATION_STATUS.md  Requirement-by-requirement coverage
    ├── PRD.md, TRD.md, ARCHITECTURE.md, API.md, DATABASE.md
    ├── SECURITY.md, TESTING.md, DEPLOYMENT.md, OPERATIONS.md, DISASTER_RECOVERY.md
    └── PROJECT_TREE.md         This file
```
