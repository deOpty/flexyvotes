from datetime import timedelta

from celery import shared_task
from django.utils import timezone


@shared_task(name='payments.tasks.reconcile_recent')
def reconcile_recent():
    """Scheduled reconciliation against the Paystack API (catches missed,
    duplicated or delayed webhooks and application downtime)."""
    from .service import reconcile

    now = timezone.now()
    run = reconcile(now - timedelta(hours=6), now)
    return {'run': run.pk, 'status': run.status, 'discrepancies': run.discrepancy_count}


@shared_task(name='payments.tasks.expire_abandoned')
def expire_abandoned():
    from .service import expire_abandoned as expire

    return expire()


@shared_task(name='payments.tasks.verify_payment')
def verify_payment(reference):
    from .service import verify_and_apply

    payment = verify_and_apply(reference, 'CALLBACK')
    return getattr(payment, 'status', None)
