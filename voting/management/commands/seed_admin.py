import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Creates a Django superuser from the DJANGO_SUPERUSER_USERNAME / "
        "DJANGO_SUPERUSER_EMAIL / DJANGO_SUPERUSER_PASSWORD environment "
        "variables. If that username already exists, its password/email/"
        "superuser+staff flags are synced to match the current environment "
        "values - so rotating DJANGO_SUPERUSER_PASSWORD in .env and "
        "restarting always takes effect, instead of silently keeping "
        "whatever password the account was first created with. Safe to run "
        "repeatedly (e.g. on every container start)."
    )

    def handle(self, *args, **options):
        # .strip() guards against a value copied from a CRLF-terminated
        # .env file (common when edited on Windows) carrying a trailing
        # \r/\n - that would otherwise seed an account whose real password
        # never matches what a human re-types from the same file.
        username = (os.getenv('DJANGO_SUPERUSER_USERNAME') or '').strip()
        password = (os.getenv('DJANGO_SUPERUSER_PASSWORD') or '').strip()
        email = (os.getenv('DJANGO_SUPERUSER_EMAIL') or '').strip()

        if not username or not password:
            self.stdout.write(
                'DJANGO_SUPERUSER_USERNAME/DJANGO_SUPERUSER_PASSWORD not set - skipping admin seed.'
            )
            return

        user, created = User.objects.get_or_create(username=username, defaults={'email': email})
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
