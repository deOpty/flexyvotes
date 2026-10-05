import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name='core.tasks.housekeeping')
def housekeeping():
    from elections.models import VoteAuthorization

    from .models import IdempotencyRecord, OTPChallenge, UserSession

    now = timezone.now()
    removed = {
        'idempotency': IdempotencyRecord.objects.filter(expires_at__lt=now).delete()[0],
        'otp': OTPChallenge.objects.filter(expires_at__lt=now - timedelta(days=1)).delete()[0],
        'sessions': UserSession.objects.filter(last_seen_at__lt=now - timedelta(days=90)).delete()[0],
        'authorizations_expired': VoteAuthorization.objects.filter(
            status=VoteAuthorization.Status.ISSUED, expires_at__lt=now,
        ).update(status=VoteAuthorization.Status.EXPIRED),
    }
    logger.info('Housekeeping complete: %s', removed)
    return removed


@shared_task(name='core.tasks.verify_audit_chains')
def verify_audit_chains():
    """Detect tampering early: re-verify every chain and raise the alarm."""
    from . import audit

    results = audit.verify_all()
    broken = {chain: result for chain, result in results.items() if not result[0]}
    if broken:
        logger.critical('AUDIT CHAIN VERIFICATION FAILED: %s', broken)
        audit.record('AUDIT_CHAIN_BROKEN', result='FAILURE', summary='Audit chain verification failed',
                     metadata={chain: {'seq': r[2], 'message': r[3]} for chain, r in broken.items()})
        from notifications.service import notify_platform_admins

        notify_platform_admins('security_alert', {
            'title': 'Audit log integrity check failed',
            'detail': '; '.join(f'{c}: {r[3]} (seq {r[2]})' for c, r in broken.items()),
        })
    return {chain: result[0] for chain, result in results.items()}
