from datetime import timedelta

from celery import shared_task
from django.utils import timezone

MAX_ATTEMPTS = 5


@shared_task(name='notifications.tasks.deliver', bind=True, max_retries=MAX_ATTEMPTS, acks_late=True)
def deliver(self, notification_id):
    from . import service
    from .models import Notification

    notification = service.deliver(notification_id)
    if notification is not None and notification.status == Notification.Status.FAILED \
            and notification.attempts < MAX_ATTEMPTS and not self.request.is_eager:
        raise self.retry(countdown=min(60 * 2 ** notification.attempts, 3600))
    return notification.status if notification else None


@shared_task(name='notifications.tasks.retry_failed')
def retry_failed():
    """Sweep for anything stuck (worker crash, broker outage)."""
    from .models import Notification

    cutoff = timezone.now() - timedelta(minutes=5)
    pending = Notification.objects.filter(status__in=[Notification.Status.QUEUED, Notification.Status.FAILED],
                                          attempts__lt=MAX_ATTEMPTS, created_at__lt=cutoff)
    count = 0
    for notification_id in pending.values_list('pk', flat=True)[:500]:
        deliver.delay(str(notification_id))
        count += 1
    return count
