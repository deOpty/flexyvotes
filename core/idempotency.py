"""Idempotency keys for payment and vote operations.

A repeated request with the same key returns the stored outcome of the first
request instead of performing the operation again. Reusing a key with a
different payload is rejected.
"""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from .crypto import canonical_json, sha256_hex

DEFAULT_TTL = timedelta(hours=24)


class IdempotencyConflict(Exception):
    """Key reused with a different request payload."""


class IdempotencyInProgress(Exception):
    """The original request with this key has not finished yet."""


def request_fingerprint(payload):
    return sha256_hex(canonical_json(payload))


def valid_key(key):
    return bool(key) and 8 <= len(key) <= 128 and all(c.isalnum() or c in '-_:.' for c in key)


def begin(scope, key, payload, ttl=DEFAULT_TTL):
    """Claim a key. Returns (record, replay) where replay is True when a
    completed response already exists for this key."""
    from .models import IdempotencyRecord

    fingerprint = request_fingerprint(payload)
    now = timezone.now()
    try:
        with transaction.atomic():
            record = IdempotencyRecord.objects.create(
                scope=scope, key=key, request_hash=fingerprint, expires_at=now + ttl,
            )
        return record, False
    except IntegrityError:
        pass
    with transaction.atomic():
        record = IdempotencyRecord.objects.select_for_update().get(scope=scope, key=key)
        if record.expires_at <= now:
            record.request_hash = fingerprint
            record.state = IdempotencyRecord.State.IN_PROGRESS
            record.response_status = None
            record.response_body = None
            record.expires_at = now + ttl
            record.save()
            return record, False
    if record.request_hash != fingerprint:
        raise IdempotencyConflict('Idempotency key was already used with a different request.')
    if record.state != IdempotencyRecord.State.COMPLETED:
        raise IdempotencyInProgress('A request with this idempotency key is still being processed.')
    return record, True


def complete(record, status, body):
    record.state = record.State.COMPLETED
    record.response_status = status
    record.response_body = body
    record.save(update_fields=['state', 'response_status', 'response_body'])


def abandon(record):
    """Release a key whose operation failed before producing a result."""
    if record is not None and record.pk:
        type(record).objects.filter(pk=record.pk, state=record.State.IN_PROGRESS).delete()
