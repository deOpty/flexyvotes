"""Tamper-evident (hash-chained, append-only) audit logging.

Every security-sensitive operation calls :func:`record`. Each chain (one per
organization plus a platform chain) is a linked list of SHA-256 hashes, so
editing or deleting any historic row breaks verification of every later row.
"""
import json
import logging
from datetime import timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.utils import timezone

from .crypto import canonical_json, sha256_hex
from .observability import get_correlation_id
from .utils import client_ip, user_agent

logger = logging.getLogger('flexyvotes.audit')

GENESIS = '0' * 64

HASHED_FIELDS = (
    'chain', 'seq', 'event_type', 'actor_id', 'actor_label', 'organization_id', 'election_id', 'target_type',
    'target_id', 'summary', 'ip_address', 'user_agent', 'correlation_id', 'changes', 'metadata', 'result',
    'reason', 'created_at',
)


def _normalize(value):
    """Round-trip through canonical JSON so what we hash is what we store."""
    return json.loads(canonical_json(value or {}))


def compute_hash(prev_hash, fields):
    payload = {name: fields.get(name) for name in HASHED_FIELDS}
    created = payload.get('created_at')
    if hasattr(created, 'astimezone'):
        payload['created_at'] = created.astimezone(dt_timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    return sha256_hex(prev_hash.encode('ascii') + canonical_json(payload))


def chain_for(organization_id):
    return f'org:{organization_id}' if organization_id else 'platform'


def diff(before, after):
    """{'field': {'old': x, 'new': y}} for fields whose value changed."""
    changes = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old != new:
            changes[key] = {'old': old, 'new': new}
    return _normalize(changes)


def snapshot(instance, fields):
    data = {}
    for name in fields:
        value = getattr(instance, name, None)
        if hasattr(value, 'pk'):
            value = value.pk
        elif hasattr(value, 'isoformat'):
            value = value.isoformat()
        elif hasattr(value, 'name') and hasattr(value, 'url'):
            value = value.name or None
        elif value is not None and not isinstance(value, (str, int, float, bool, list, dict)):
            value = str(value)
        data[name] = value
    return data


def _head_for_update(chain):
    from .models import AuditChainHead

    for _ in range(3):
        try:
            with transaction.atomic():
                AuditChainHead.objects.get_or_create(chain=chain)
            break
        except IntegrityError:
            continue
    return AuditChainHead.objects.select_for_update().get(chain=chain)


def record(event_type, *, request=None, actor=None, event=None, organization=None, organization_id=None,
           target=None, target_type='', target_id='', summary='', changes=None, metadata=None,
           result='SUCCESS', reason='', ip_address=None):
    from .models import AuditEvent

    if actor is None and request is not None and getattr(request, 'user', None) is not None \
            and request.user.is_authenticated:
        actor = request.user
    if organization_id is None:
        if organization is not None:
            organization_id = organization.pk
        elif event is not None:
            organization_id = event.organization_id
    if target is not None:
        target_type = target_type or target._meta.label
        target_id = target_id or str(target.pk)
    elif event is not None and not target_type:
        target_type, target_id = 'voting.Event', str(event.pk)

    correlation = get_correlation_id()
    fields = {
        'chain': chain_for(organization_id),
        'event_type': event_type,
        'actor_id': getattr(actor, 'pk', None),
        'actor_label': (getattr(actor, 'username', None) or (str(actor) if actor else ''))[:150],
        'organization_id': organization_id,
        'election_id': event.pk if event is not None else None,
        'target_type': (target_type or '')[:64],
        'target_id': str(target_id or '')[:64],
        'summary': (summary or '')[:500],
        'ip_address': ip_address or client_ip(request),
        'user_agent': user_agent(request),
        'correlation_id': '' if correlation == '-' else correlation,
        'changes': _normalize(changes),
        'metadata': _normalize(metadata),
        'result': result,
        'reason': reason or '',
        'created_at': timezone.now(),
    }
    with transaction.atomic():
        head = _head_for_update(fields['chain'])
        fields['seq'] = head.seq + 1
        fields['prev_hash'] = head.last_hash
        fields['hash'] = compute_hash(head.last_hash, fields)
        entry = AuditEvent.objects.create(**fields)
        type(head).objects.filter(pk=head.pk).update(seq=fields['seq'], last_hash=fields['hash'])
    logger.info('audit %s actor=%s election=%s result=%s', event_type, fields['actor_label'] or 'system',
                fields['election_id'], result)
    return entry


def verify_chain(chain):
    """Return (ok, checked_count, first_bad_seq, message)."""
    from .models import AuditChainHead, AuditEvent

    prev = GENESIS
    expected_seq = 1
    count = 0
    for entry in AuditEvent.objects.filter(chain=chain).order_by('seq').iterator():
        fields = {name: getattr(entry, name) for name in HASHED_FIELDS}
        if entry.seq != expected_seq:
            return False, count, entry.seq, f'Sequence gap: expected {expected_seq}, found {entry.seq}.'
        if entry.prev_hash != prev:
            return False, count, entry.seq, 'Previous-hash link broken.'
        if compute_hash(prev, fields) != entry.hash:
            return False, count, entry.seq, 'Content hash mismatch (row was modified).'
        prev = entry.hash
        expected_seq += 1
        count += 1
    head = AuditChainHead.objects.filter(chain=chain).first()
    if head and (head.seq != count or head.last_hash != prev):
        return False, count, head.seq, 'Chain head does not match the last entry (rows deleted?).'
    return True, count, None, 'Chain intact.'


def verify_all():
    from .models import AuditChainHead

    return {head.chain: verify_chain(head.chain) for head in AuditChainHead.objects.all()}
