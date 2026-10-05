from django.db.models.signals import post_migrate
from django.dispatch import receiver


@receiver(post_migrate)
def seed_plans(sender, **kwargs):
    if sender.name != 'billing':
        return
    from .service import sync_plans

    sync_plans()
