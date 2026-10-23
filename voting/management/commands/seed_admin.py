import os
import re

from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand

EXTRA_ADMIN_RE = re.compile(r'^DJANGO_SUPERUSER_(\d+)_USERNAME$')


def _env(name):
    # .strip() guards against a value copied from a CRLF-terminated .env file
    # (common when edited on Windows) carrying a trailing \r/\n - that would
    # otherwise seed an account whose real password never matches what a
    # human re-types from the same file.
    return (os.getenv(name) or '').strip()


def configured_admins():
    """The first admin (DJANGO_SUPERUSER_*) plus any number of extra ones
    (DJANGO_SUPERUSER_2_*, DJANGO_SUPERUSER_3_*, ...), in order."""
    admins = [('DJANGO_SUPERUSER', _env('DJANGO_SUPERUSER_USERNAME'), _env('DJANGO_SUPERUSER_EMAIL'),
               _env('DJANGO_SUPERUSER_PASSWORD'))]
    numbers = sorted({int(m.group(1)) for m in map(EXTRA_ADMIN_RE.match, os.environ) if m})
    for n in numbers:
        prefix = f'DJANGO_SUPERUSER_{n}'
        admins.append((prefix, _env(f'{prefix}_USERNAME'), _env(f'{prefix}_EMAIL'), _env(f'{prefix}_PASSWORD')))
    return admins


class Command(BaseCommand):
    help = (
        "Creates or syncs Django superusers from the environment: the first admin from "
        "DJANGO_SUPERUSER_USERNAME / _EMAIL / _PASSWORD, and extra admins from "
        "DJANGO_SUPERUSER_2_USERNAME / _2_EMAIL / _2_PASSWORD, DJANGO_SUPERUSER_3_..., and so on. "
        "An existing account's password, email and superuser/staff flags are synced to the current "
        "values, so rotating a password in .env and restarting always takes effect. Safe to run "
        "repeatedly (it runs on every web container start). Removing a block from .env does not "
        "delete the account - deactivate it in the admin instead."
    )

    def handle(self, *args, **options):
        seeded = 0
        for prefix, username, email, password in configured_admins():
            if not username and not password:
                continue
            if not username or not password:
                self.stderr.write(self.style.ERROR(f'{prefix}: both _USERNAME and _PASSWORD are required - skipped.'))
                continue
            seeded += self.seed(prefix, username, email, password)
        if not seeded:
            self.stdout.write('No admin accounts configured (DJANGO_SUPERUSER_*) - skipping admin seed.')

    def seed(self, prefix, username, email, password):
        user = User.objects.filter(username=username).first()
        try:
            validate_password(password, user=user or User(username=username, email=email))
        except ValidationError as exc:
            self.stderr.write(self.style.ERROR(
                f'{prefix}: password for "{username}" rejected ({" ".join(exc.messages)}) - account not changed.'))
            return 0
        created = user is None
        if created:
            user = User(username=username)
        user.email = email
        user.set_password(password)
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.save()
        if created:
            self.stdout.write(self.style.SUCCESS(f'Created superuser "{username}".'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Synced superuser "{username}" to current environment values.'))
        return 1
