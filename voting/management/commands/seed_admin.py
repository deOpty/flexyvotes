import os

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = (
        "Creates a Django superuser from the DJANGO_SUPERUSER_USERNAME / "
        "DJANGO_SUPERUSER_EMAIL / DJANGO_SUPERUSER_PASSWORD environment "
        "variables, if one with that username doesn't already exist. "
        "Safe to run repeatedly (e.g. on every container start)."
    )

    def handle(self, *args, **options):
        username = os.getenv('DJANGO_SUPERUSER_USERNAME')
        password = os.getenv('DJANGO_SUPERUSER_PASSWORD')
        email = os.getenv('DJANGO_SUPERUSER_EMAIL', '')

        if not username or not password:
            self.stdout.write(
                'DJANGO_SUPERUSER_USERNAME/DJANGO_SUPERUSER_PASSWORD not set - skipping admin seed.'
            )
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write(f'Superuser "{username}" already exists - skipping.')
            return

        User.objects.create_superuser(username=username, email=email, password=password)
        self.stdout.write(self.style.SUCCESS(f'Created superuser "{username}".'))
