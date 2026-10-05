"""Structured logging, correlation IDs, error tracking and tracing setup.

This module is imported by settings, so it must not touch Django models or
anything that requires the app registry.
"""
import contextvars
import logging
import uuid

correlation_id_var = contextvars.ContextVar('correlation_id', default='-')


def new_correlation_id():
    return uuid.uuid4().hex


def get_correlation_id():
    return correlation_id_var.get()


class CorrelationIdFilter(logging.Filter):
    """Stamp every log record with the current request's correlation id."""

    def filter(self, record):
        record.correlation_id = correlation_id_var.get()
        return True


def configure_observability(sentry_dsn=None, otel_endpoint=None, environment='production', release='dev'):
    if sentry_dsn:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.celery import CeleryIntegration
            from sentry_sdk.integrations.django import DjangoIntegration

            sentry_sdk.init(
                dsn=sentry_dsn,
                integrations=[DjangoIntegration(), CeleryIntegration()],
                environment=environment,
                release=release,
                traces_sample_rate=0.05,
                # Never ship voter PII / ballot data to a third party.
                send_default_pii=False,
            )
        except ImportError:  # pragma: no cover - optional dependency
            logging.getLogger(__name__).warning('sentry-sdk not installed; error tracking disabled')

    if otel_endpoint:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.instrumentation.django import DjangoInstrumentor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({
                'service.name': 'flexyvotes', 'service.version': release, 'deployment.environment': environment,
            }))
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f'{otel_endpoint.rstrip("/")}/v1/traces')))
            trace.set_tracer_provider(provider)
            DjangoInstrumentor().instrument()
        except ImportError:  # pragma: no cover - optional dependency
            logging.getLogger(__name__).warning('OpenTelemetry packages not installed; tracing disabled')
