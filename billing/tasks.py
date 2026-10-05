from celery import shared_task


@shared_task(name='billing.tasks.generate_due_invoices')
def generate_due_invoices():
    from .service import renew_due_subscriptions

    return renew_due_subscriptions()
