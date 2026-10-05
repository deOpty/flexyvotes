"""Election integrity controls: signed configuration snapshots, freezes,
dual-approval requests, disputes, incidents and evidence preservation."""
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone

from core import audit, crypto
from core.rbac import check_perm, has_perm
from core.storage import file_sha256
from voting.models import Event

from . import lifecycle
from .models import ApprovalRequest, CaseNote, Dispute, ElectionConfigSnapshot, EvidenceItem, Incident

FREEZE_FLAGS = {
    'config': 'config_frozen', 'ballot': 'ballot_frozen', 'candidates': 'candidates_frozen',
    'voters': 'voter_list_frozen',
}


class IntegrityControlError(Exception):
    pass


# ---------------------------------------------------------------------------
# Signed configuration
# ---------------------------------------------------------------------------
def configuration_document(event):
    from .models import Voter

    roll = sorted(event.voters.exclude(credential_hash__isnull=True).values_list('credential_hash', flat=True))
    voters = event.voters.count()
    return {
        'election': {
            'id': event.pk, 'title': event.title, 'mode': event.voting_mode, 'code_mode': event.code_voting_mode,
            'start': event.start_date.isoformat(), 'end': event.end_date.isoformat(), 'timezone': event.timezone,
            'currency': event.currency, 'results_visibility': event.results_visibility,
            'auth_methods': sorted(event.auth_methods or []), 'require_second_factor': event.require_second_factor,
            'self_registration': event.allow_self_registration, 'dual_approval': event.dual_approval_required,
            'key_custody': event.key_custody, 'trustee_threshold': event.trustee_threshold,
            'vote_price': str(event.vote_price), 'max_votes_per_voter': event.max_votes_per_voter,
            'record_constituency_on_ballot': event.record_constituency_on_ballot,
            'min_anonymity_set': event.min_anonymity_set,
        },
        'positions': [
            {
                'id': p.pk, 'name': p.name, 'type': p.ballot_type, 'min': p.min_select, 'max': p.max_select,
                'seats': p.seats, 'max_score': p.max_score, 'abstain': p.allow_abstain,
                'threshold': str(p.referendum_threshold), 'constituency': p.constituency_id,
                'candidates': [{'id': c.pk, 'name': c.name, 'status': c.status} for c in p.candidates.order_by('pk')],
            }
            for p in event.categories.order_by('pk')
        ],
        'uncategorized_candidates': [{'id': c.pk, 'name': c.name} for c in event.candidates.filter(category__isnull=True).order_by('pk')],
        'eligibility_rules': [
            {'id': r.pk, 'kind': r.kind, 'position': r.position_id, 'attribute': r.attribute, 'values': r.values,
             'constituency': r.constituency_id, 'active': r.is_active}
            for r in event.eligibility_rules.order_by('pk')
        ],
        'voter_roll': {'count': voters, 'credentials_digest': crypto.sha256_hex('|'.join(roll)),
                       'statuses': {s: event.voters.filter(status=s).count() for s in Voter.Status.values}},
    }


def snapshot_configuration(event, actor, reason):
    document = configuration_document(event)
    encoded = crypto.canonical_json(document)
    version = (event.config_snapshots.order_by('-version').values_list('version', flat=True).first() or 0) + 1
    public = crypto.public_key_b64()
    snapshot = ElectionConfigSnapshot.objects.create(
        election=event, version=version, reason=reason, config=document, config_hash=crypto.sha256_hex(encoded),
        signature=crypto.sign(encoded), public_key=public, key_id=crypto.key_fingerprint(public), created_by=actor,
    )
    audit.record('ELECTION_CONFIG_SIGNED', actor=actor, event=event, target=snapshot,
                 summary=f'Configuration v{version} signed ({reason})', metadata={'config_hash': snapshot.config_hash})
    return snapshot


def verify_snapshot(snapshot):
    encoded = crypto.canonical_json(snapshot.config)
    return (crypto.sha256_hex(encoded) == snapshot.config_hash
            and crypto.verify_signature(encoded, snapshot.signature, snapshot.public_key))


# ---------------------------------------------------------------------------
# Freezes
# ---------------------------------------------------------------------------
def freeze(event, scope, actor, request=None):
    check_perm(actor, 'election.freeze', event)
    flag = FREEZE_FLAGS[scope]
    if getattr(event, flag):
        return event
    Event.objects.filter(pk=event.pk).update(**{flag: True})
    setattr(event, flag, True)
    audit.record('ELECTION_FROZEN', request=request, actor=actor, event=event, summary=f'{scope} frozen',
                 changes={flag: {'old': False, 'new': True}})
    return event


def unfreeze(event, scope, actor, reason, request=None):
    """Lifting a freeze outside DRAFT is a sensitive action (dual approval)."""
    check_perm(actor, 'election.freeze', event)
    if event.status in lifecycle.LOCKED_STATES:
        raise lifecycle.LifecycleError('Freezes cannot be lifted once voting has closed.')
    return request_approval(event, ApprovalRequest.Action.UNFREEZE, {'scope': scope}, reason, actor, request)


# ---------------------------------------------------------------------------
# Dual approval
# ---------------------------------------------------------------------------
ACTION_PERMS = {
    ApprovalRequest.Action.UNFREEZE: 'election.freeze',
    ApprovalRequest.Action.EXTEND_VOTING: 'election.close',
    ApprovalRequest.Action.REOPEN_VOTING: 'election.close',
    ApprovalRequest.Action.BULK_CREDENTIAL_RESET: 'voter.credentials',
    ApprovalRequest.Action.DECERTIFY: 'results.certify',
    ApprovalRequest.Action.RELEASE_LEGAL_HOLD: 'dispute.manage',
    ApprovalRequest.Action.REFUND: 'refund.create',
}


def _exec_unfreeze(req, approver):
    flag = FREEZE_FLAGS[req.payload['scope']]
    Event.objects.filter(pk=req.election_id).update(**{flag: False})
    return {'unfrozen': flag}


def _parse_dt(value):
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(value) if isinstance(value, str) else value
    if parsed is None:
        raise IntegrityControlError('Invalid date/time.')
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _exec_extend(req, approver):
    event = Event.objects.get(pk=req.election_id)
    new_end = _parse_dt(req.payload['new_end'])
    if new_end <= timezone.now() or new_end <= event.end_date:
        raise IntegrityControlError('The new end time must be later than both now and the current end time.')
    if event.status not in (Event.Status.OPEN, Event.Status.PAUSED, Event.Status.SCHEDULED):
        raise IntegrityControlError('Voting can only be extended while scheduled, open or paused.')
    old = event.end_date
    Event.objects.filter(pk=event.pk).update(end_date=new_end)
    return {'old_end': old.isoformat(), 'new_end': new_end.isoformat()}


def _exec_reopen(req, approver):
    event = Event.objects.get(pk=req.election_id)
    new_end = _parse_dt(req.payload['new_end'])
    if new_end <= timezone.now():
        raise IntegrityControlError('The new end time must be in the future.')
    if event.results.filter(status='APPROVED').exists():
        raise IntegrityControlError('Results were already certified; voting cannot be re-opened.')

    def effect(locked):
        locked.end_date = new_end
        locked.closed_at = None

    lifecycle.transition(event, 'reopen_voting', actor=approver, system=True, reason=req.reason, extra_effect=effect)
    return {'new_end': new_end.isoformat()}


def _exec_bulk_reset(req, approver):
    from .voters import reset_all_unused_credentials

    return {'reset': reset_all_unused_credentials(Event.objects.get(pk=req.election_id), approver)}


def _exec_decertify(req, approver):
    from .results import revoke_certification

    revoke_certification(Event.objects.get(pk=req.election_id), approver, req.reason)
    return {'decertified': True}


def _exec_release_hold(req, approver):
    Event.objects.filter(pk=req.election_id).update(legal_hold=False)
    return {'legal_hold': False}


def _exec_refund(req, approver):
    from payments.service import approve_refund

    refund = approve_refund(req.payload['refund_id'], approver, via_approval=True)
    return {'refund': str(refund.pk), 'status': refund.status}


EXECUTORS = {
    ApprovalRequest.Action.UNFREEZE: _exec_unfreeze,
    ApprovalRequest.Action.EXTEND_VOTING: _exec_extend,
    ApprovalRequest.Action.REOPEN_VOTING: _exec_reopen,
    ApprovalRequest.Action.BULK_CREDENTIAL_RESET: _exec_bulk_reset,
    ApprovalRequest.Action.DECERTIFY: _exec_decertify,
    ApprovalRequest.Action.RELEASE_LEGAL_HOLD: _exec_release_hold,
    ApprovalRequest.Action.REFUND: _exec_refund,
}


def _execute(req, approver, request=None):
    try:
        with transaction.atomic():
            result = EXECUTORS[req.action](req, approver)
        req.status = ApprovalRequest.Status.EXECUTED
        req.result = result
        req.executed_at = timezone.now()
        outcome = 'SUCCESS'
    except Exception as exc:  # noqa: BLE001 - recorded on the request and in the audit log
        req.status = ApprovalRequest.Status.FAILED
        req.result = {'error': str(exc)}
        outcome = 'FAILURE'
    req.save()
    audit.record(f'APPROVAL_{req.action}_EXECUTED', request=request, actor=approver, event=req.election,
                 organization_id=req.organization_id, target=req, result=outcome, reason=req.reason,
                 summary=f'{req.get_action_display()} {"executed" if outcome == "SUCCESS" else "failed"}',
                 metadata={'payload': req.payload, 'result': req.result})
    return req


def request_approval(event, action, payload, reason, requester, request=None, organization=None):
    perm = ACTION_PERMS[action]
    scope = event if event is not None else organization
    check_perm(requester, perm, scope)
    if not reason or not reason.strip():
        raise IntegrityControlError('A reason is required for this action.')
    req = ApprovalRequest.objects.create(
        election=event, organization=organization or (event.organization if event else None), action=action,
        payload=payload, reason=reason.strip(), requested_by=requester, expires_at=timezone.now() + timedelta(days=3),
    )
    audit.record(f'APPROVAL_{action}_REQUESTED', request=request, actor=requester, event=event,
                 organization_id=req.organization_id, target=req, reason=reason, summary=f'{req.get_action_display()} requested',
                 metadata={'payload': payload})
    dual = event.dual_approval_required if event is not None else True
    if not dual:
        req.decided_by = requester
        req.decided_at = timezone.now()
        req.decision_note = 'Dual approval not required for this election.'
        req.status = ApprovalRequest.Status.APPROVED
        req.save()
        return _execute(req, requester, request)
    from notifications.service import notify_approvers

    transaction.on_commit(lambda: notify_approvers(req))
    return req


def decide(req, approver, approve, note='', request=None):
    if req.status != ApprovalRequest.Status.PENDING:
        raise IntegrityControlError('This request has already been decided.')
    if req.expires_at <= timezone.now():
        req.status = ApprovalRequest.Status.EXPIRED
        req.save(update_fields=['status'])
        raise IntegrityControlError('This request has expired.')
    scope = req.election if req.election_id else req.organization
    if not has_perm(approver, 'approval.decide', scope):
        raise PermissionDenied
    if approver.pk == req.requested_by_id:
        raise IntegrityControlError('Separation of duties: you cannot approve your own request.')
    req.decided_by = approver
    req.decided_at = timezone.now()
    req.decision_note = note
    req.status = ApprovalRequest.Status.APPROVED if approve else ApprovalRequest.Status.REJECTED
    req.save()
    audit.record(f'APPROVAL_{req.action}_{"APPROVED" if approve else "REJECTED"}', request=request, actor=approver,
                 event=req.election, organization_id=req.organization_id, target=req, reason=note,
                 summary=f'{req.get_action_display()} {"approved" if approve else "rejected"}')
    if approve:
        return _execute(req, approver, request)
    return req


# ---------------------------------------------------------------------------
# Disputes, incidents, evidence
# ---------------------------------------------------------------------------
def file_dispute(event, *, filer_name, filer_email, filer_role, category, subject, description, user=None, request=None):
    dispute = Dispute.objects.create(election=event, filed_by=user if user and user.is_authenticated else None,
                                     filer_name=filer_name, filer_email=filer_email, filer_role=filer_role,
                                     category=category, subject=subject, description=description)
    audit.record('DISPUTE_FILED', request=request, event=event, target=dispute, summary=f'Dispute {dispute.reference} filed',
                 metadata={'category': category, 'role': filer_role})
    return dispute


def update_dispute(dispute, actor, status, resolution='', request=None):
    check_perm(actor, 'dispute.manage', dispute.election)
    old = dispute.status
    dispute.status = status
    if resolution:
        dispute.resolution = resolution
    if status in (Dispute.Status.UPHELD, Dispute.Status.DISMISSED, Dispute.Status.WITHDRAWN):
        dispute.resolved_by = actor
        dispute.resolved_at = timezone.now()
    dispute.save()
    audit.record('DISPUTE_UPDATED', request=request, actor=actor, event=dispute.election, target=dispute,
                 changes={'status': {'old': old, 'new': status}}, reason=resolution)
    return dispute


def open_incident(*, title, description, severity, actor, event=None, organization=None, impact='', request=None):
    scope = event or organization
    check_perm(actor, 'incident.manage', scope)
    incident = Incident.objects.create(election=event, organization=organization or (event.organization if event else None),
                                       title=title, description=description, severity=severity, impact=impact,
                                       reported_by=actor)
    audit.record('INCIDENT_OPENED', request=request, actor=actor, event=event, target=incident,
                 organization_id=incident.organization_id, summary=f'{incident.reference}: {title}',
                 metadata={'severity': severity})
    if severity in (Incident.Severity.HIGH, Incident.Severity.CRITICAL):
        from notifications.service import notify_platform_admins

        transaction.on_commit(lambda: notify_platform_admins('security_alert', {
            'title': f'{severity} incident {incident.reference}', 'detail': title}))
    return incident


def update_incident(incident, actor, status, note='', request=None):
    check_perm(actor, 'incident.manage', incident.election or incident.organization)
    old = incident.status
    incident.status = status
    if status in (Incident.Status.RESOLVED, Incident.Status.CLOSED) and not incident.resolved_at:
        incident.resolved_at = timezone.now()
    incident.save()
    if note:
        CaseNote.objects.create(incident=incident, author=actor, body=note)
    audit.record('INCIDENT_UPDATED', request=request, actor=actor, event=incident.election, target=incident,
                 organization_id=incident.organization_id, changes={'status': {'old': old, 'new': status}}, reason=note)
    return incident


def add_note(actor, body, dispute=None, incident=None, request=None):
    note = CaseNote.objects.create(dispute=dispute, incident=incident, author=actor, body=body)
    target = dispute or incident
    audit.record('CASE_NOTE_ADDED', request=request, actor=actor, event=target.election, target=target,
                 summary=f'Note added to {target.reference}')
    return note


def preserve_evidence(event, uploaded, title, actor, description='', dispute=None, incident=None, request=None):
    check_perm(actor, 'dispute.manage', event) if dispute else check_perm(actor, 'incident.manage', event)
    digest = file_sha256(uploaded)
    item = EvidenceItem.objects.create(election=event, dispute=dispute, incident=incident, title=title,
                                       description=description, file=uploaded, sha256=digest, size=uploaded.size,
                                       uploaded_by=actor)
    audit.record('EVIDENCE_PRESERVED', request=request, actor=actor, event=event, target=item,
                 summary=f'Evidence "{title}" preserved', metadata={'sha256': digest, 'size': uploaded.size})
    return item


def evidence_intact(item):
    import hashlib

    digest = hashlib.sha256()
    with item.file.open('rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest() == item.sha256


def set_legal_hold(event, actor, enabled, reason, request=None):
    if enabled:
        check_perm(actor, 'dispute.manage', event)
        Event.objects.filter(pk=event.pk).update(legal_hold=True)
        audit.record('LEGAL_HOLD_SET', request=request, actor=actor, event=event, reason=reason,
                     summary='Legal hold placed (evidence preservation)')
        return None
    return request_approval(event, ApprovalRequest.Action.RELEASE_LEGAL_HOLD, {}, reason, actor, request)
