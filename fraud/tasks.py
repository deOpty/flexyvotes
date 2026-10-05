from celery import shared_task


@shared_task(name='fraud.tasks.anomaly_scan')
def anomaly_scan():
    from .engine import anomaly_scan as scan

    return scan()
