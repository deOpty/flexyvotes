# Project Tree

This document maps the FlexyVotes repository layout. Generated/ignored paths
(`.venv/`, `__pycache__/`, `.git/`, `media/` contents, `staticfiles/`) are
omitted from the detailed tree but noted where relevant.

```
flexyvotes/
├── .env                        # Local secrets (gitignored, not committed)
├── .env.example                # Template listing every required env var
├── .gitignore
├── .dockerignore
├── Dockerfile                   # Image build: deps, collectstatic, non-root user, healthcheck, gunicorn
├── docker-entrypoint.sh         # Runs migrate + seed_admin, then execs the given command
├── docker-compose.yml           # Postgres + web + pgAdmin (DB admin UI) for testing before AWS
├── README.md                   # Project overview, setup, env vars (repo landing page)
├── docs/                       # Additional project documentation
│   ├── PRD.md
│   ├── TRD.md
│   ├── ARCHITECTURE.md
│   ├── API.md
│   ├── DATABASE.md
│   ├── TESTING.md
│   ├── SECURITY.md
│   ├── DEPLOYMENT.md
│   ├── OPERATIONS.md
│   └── PROJECT_TREE.md         # This file
├── manage.py                   # Django management CLI entrypoint
├── Procfile                    # Process command for PaaS platforms (gunicorn)
├── requirements.txt            # Pinned Python dependencies
├── runtime.txt                 # Python version pin (python-3.12.4)
├── db.sqlite3                  # Local dev database (gitignored)
├── media/                      # Local media root (gitignored; production uses Cloudinary)
│   ├── candidate_images/
│   ├── event_backgrounds/
│   ├── event_flyers/
│   ├── product_images/
│   └── ticket_images/
├── templates/
│   └── voting/                 # All server-rendered HTML templates
│       ├── base.html           # Shared layout/nav
│       ├── home.html           # Public event listing
│       ├── event_detail.html   # Event + candidates + voting UI
│       ├── login.html / register.html
│       ├── dashboard.html      # Organizer/admin dashboard
│       ├── create_event.html / edit_event.html
│       ├── edit_candidate.html / edit_category.html
│       ├── analytics.html      # Per-event vote/revenue analytics
│       ├── store.html / manage_store.html / add_product.html / edit_product.html
│       ├── tickets.html / event_tickets.html / create_ticket.html / edit_ticket.html
│       ├── ticket_success.html / verify_ticket.html / retrieve_ticket.html
│       ├── guestlist.html      # Organizer guest list + check-in export
│       ├── scanner.html        # QR check-in camera scanner (organizer/staff)
│       ├── payment_success.html
│       └── contact.html
├── vote_fund/                   # Django project package (settings/config)
│   ├── __init__.py
│   ├── settings.py             # All configuration; reads from .env
│   ├── urls.py                 # Root URL routing (mounts voting.urls, admin, USSD)
│   ├── wsgi.py                 # WSGI entrypoint (used by gunicorn)
│   └── asgi.py                 # ASGI entrypoint (unused in current deployment)
└── voting/                      # The single Django app containing all business logic
    ├── __init__.py
    ├── apps.py
    ├── admin.py                 # Django admin customizations (organizer approval, etc.)
    ├── models.py                # All data models (see docs/DATABASE.md)
    ├── views.py                 # All view functions (see docs/API.md)
    ├── urls.py                  # App-level URL routing
    ├── services.py              # Paystack payment initialization + verification helpers
    ├── at_service.py            # Africa's Talking mobile-money helper (currently unused by views)
    ├── tests.py                 # Automated regression/unit tests
    ├── management/
    │   └── commands/
    │       └── seed_admin.py    # Creates a superuser from DJANGO_SUPERUSER_* env vars, idempotent
    └── migrations/              # 29 migrations tracking schema evolution
        ├── 0001_initial.py
        └── ... (0002 - 0029)
```

## Notes

- **Single Django app**: All business logic lives in one app (`voting`); the
  `vote_fund` package only holds project-level configuration and routing.
- **No frontend build step**: Templates are server-rendered Django templates
  with vanilla JS in `<script>` blocks (see `scanner.html`, `ticket_success.html`)
  and Bootstrap-style classes; there is no separate SPA/bundler.
- **`media/`** currently contains real uploaded files from earlier
  development/testing. It is now gitignored going forward — production file
  storage is Cloudinary, not the local filesystem.
- **`.env`** contains live third-party credentials and must never be
  committed. It was previously tracked in git history; see `docs/SECURITY.md`
  for the required credential-rotation follow-up.
- Deployment-specific files (`Dockerfile`, `.dockerignore`, `docker-compose.yml`,
  `docker-entrypoint.sh`) live at the repository root alongside `Procfile` —
  both the Docker entrypoint and `Procfile` bind to `0.0.0.0:${PORT:-8000}`,
  configurable via the `PORT` env var — see `docs/DEPLOYMENT.md`.
