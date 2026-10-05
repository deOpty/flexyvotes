"""Prometheus metrics.

With gunicorn's multiple worker processes, set PROMETHEUS_MULTIPROC_DIR (the
Docker image does) so every worker's samples are aggregated on scrape.
"""
import logging
import os

from prometheus_client import (CONTENT_TYPE_LATEST, REGISTRY, CollectorRegistry, Counter, Histogram,
                               generate_latest)
from prometheus_client.core import GaugeMetricFamily

logger = logging.getLogger(__name__)

HTTP_REQUESTS = Counter('fv_http_requests_total', 'HTTP requests', ['method', 'status_class'])
HTTP_LATENCY = Histogram('fv_http_request_duration_seconds', 'HTTP request latency', ['view'],
                         buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
DB_LATENCY = Histogram('fv_db_query_duration_seconds', 'Database query latency',
                       buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 1, 5))
VOTES = Counter('fv_votes_total', 'Votes recorded (paid votes count individually)', ['mode'])
VOTE_SUBMISSIONS = Counter('fv_vote_submissions_total', 'Vote submission attempts', ['mode', 'outcome'])
VOTE_LATENCY = Histogram('fv_vote_latency_seconds', 'Vote submission latency', ['mode'],
                         buckets=(0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5))
PAYMENTS = Counter('fv_payments_total', 'Payment state transitions', ['status'])
WEBHOOKS = Counter('fv_webhooks_total', 'Webhook deliveries', ['provider', 'outcome'])
FRAUD_ALERTS = Counter('fv_fraud_alerts_total', 'Risk assessments above the monitor threshold', ['decision'])
NOTIFICATIONS = Counter('fv_notifications_total', 'Notifications', ['channel', 'status'])
LOGINS = Counter('fv_logins_total', 'Login attempts', ['kind', 'outcome'])
RATE_LIMITED = Counter('fv_rate_limited_total', 'Requests rejected by rate limiting', ['scope'])
RECONCILIATION_DISCREPANCIES = Counter('fv_reconciliation_discrepancies_total', 'Payment reconciliation discrepancies', ['kind'])

QUEUE_NAMES = ('default', 'notifications', 'payments')


def queue_depths():
    """Pending job count per Celery queue (Redis broker only)."""
    from django.conf import settings

    broker = getattr(settings, 'CELERY_BROKER_URL', None)
    if not broker or not broker.startswith(('redis://', 'rediss://')):
        return {}
    try:
        import redis

        client = redis.Redis.from_url(broker, socket_timeout=1, socket_connect_timeout=1)
        return {name: int(client.llen(name)) for name in QUEUE_NAMES}
    except Exception:  # noqa: BLE001 - metrics must never break the scrape
        logger.warning('Could not read queue depth from broker', exc_info=True)
        return {}


class QueueDepthCollector:
    def collect(self):
        family = GaugeMetricFamily('fv_queue_depth', 'Pending background jobs', labels=['queue'])
        for name, depth in queue_depths().items():
            family.add_metric([name], depth)
        yield family


class BusinessGaugeCollector:
    """Point-in-time gauges read from the database on scrape."""

    def collect(self):
        try:
            from django.utils import timezone

            from voting.models import Event

            open_count = Event.objects.filter(status=Event.Status.OPEN).count()
            family = GaugeMetricFamily('fv_open_elections', 'Elections currently open for voting')
            family.add_metric([], open_count)
            yield family
            from fraud.models import FraudEvent

            alerts = FraudEvent.objects.filter(status=FraudEvent.Status.OPEN).count()
            family = GaugeMetricFamily('fv_open_fraud_alerts', 'Unreviewed fraud alerts')
            family.add_metric([], alerts)
            yield family
            from payments.models import WebhookEvent

            since = timezone.now() - timezone.timedelta(hours=1)
            failed = WebhookEvent.objects.filter(status=WebhookEvent.Status.FAILED, received_at__gte=since).count()
            family = GaugeMetricFamily('fv_failed_webhooks_last_hour', 'Webhooks that failed processing in the last hour')
            family.add_metric([], failed)
            yield family
        except Exception:  # noqa: BLE001
            logger.warning('Business gauge collection failed', exc_info=True)


_registered = False


def render_latest():
    global _registered
    if os.environ.get('PROMETHEUS_MULTIPROC_DIR'):
        from prometheus_client import multiprocess

        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        registry.register(QueueDepthCollector())
        registry.register(BusinessGaugeCollector())
    else:
        registry = REGISTRY
        if not _registered:
            registry.register(QueueDepthCollector())
            registry.register(BusinessGaugeCollector())
            _registered = True
    return generate_latest(registry), CONTENT_TYPE_LATEST
