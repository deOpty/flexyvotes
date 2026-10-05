"""Voter roll management."""
import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from core import audit, crypto
from core.rbac import check_perm
from voting.models import Event, hash_voting_code

from . import lifecycle
from .models import Constituency, VoteAuthorization, Voter

MAX_IMPORT_ROWS = 100000
IDENTIFIER_RE = re.compile(r'^[A-Za-z0-9._/@-]{1,100}$')
COLUMN_ALIASES = {
    'identifier': ('identifier', 'id', 'student_id', 'staff_id', 'member_id', 'voter_id', 'reference', 'index_number'),
    'full_name': ('full_name', 'name', 'fullname', 'voter_name'),
    'email': ('email', 'email_address', 'e-mail'),
    'phone': ('phone', 'phone_number', 'mobile', 'msisdn', 'telephone'),
    'constituency': ('constituency', 'constituency_code', 'department', 'faculty', 'branch', 'region', 'unit'),
}


class VoterImportError(Exception):
    pass


@dataclass
class ImportReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list = field(default_factory=list)
    codes_issued: int = 0

    def as_dict(self):
        return {'created': self.created, 'updated': self.updated, 'skipped': self.skipped,
                'codes_issued': self.codes_issued, 'errors': self.errors[:200], 'error_count': len(self.errors)}


def _canonical_columns(headers):
    mapping = {}
    for index, raw in enumerate(headers):
        name = (raw or '').strip().lower().replace(' ', '_')
        for canonical, aliases in COLUMN_ALIASES.items():
            if name in aliases and canonical not in mapping.values():
                mapping[index] = canonical
                break
        else:
            if name:
                mapping[index] = f'attr:{name}'
    return mapping


def parse_upload(uploaded):
    """Read a CSV or XLSX upload into a list of dicts with canonical keys."""
    suffix = Path(uploaded.name).suffix.lower()
    if uploaded.size > 20 * 1024 * 1024:
        raise VoterImportError('File too large (max 20 MB).')
    if suffix == '.csv':
        raw = uploaded.read()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('latin-1')
        rows = list(csv.reader(io.StringIO(text)))
    elif suffix in ('.xlsx', '.xlsm'):
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(uploaded, read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001
            raise VoterImportError('Could not read the Excel file.') from exc
        sheet = workbook.active
        rows = [['' if cell is None else str(cell).strip() for cell in row] for row in sheet.iter_rows(values_only=True)]
        workbook.close()
    else:
        raise VoterImportError('Upload a .csv or .xlsx file.')
    rows = [r for r in rows if any((c or '').strip() for c in r)]
    if not rows:
        raise VoterImportError('The file is empty.')
    if len(rows) > MAX_IMPORT_ROWS + 1:
        raise VoterImportError(f'At most {MAX_IMPORT_ROWS} voters per import.')
    header = rows[0]
    mapping = _canonical_columns(header)
    has_header = any(v in ('identifier', 'email', 'full_name') for v in mapping.values())
    if not has_header:
        # Legacy format: "identifier[,email]" without a header row.
        mapping = {0: 'identifier', 1: 'email'}
        data_rows = rows
    else:
        data_rows = rows[1:]
    parsed = []
    for row in data_rows:
        item = {'attributes': {}}
        for index, value in enumerate(row):
            key = mapping.get(index)
            value = (value or '').strip()
            if not key or not value:
                continue
            if key.startswith('attr:'):
                item['attributes'][key[5:]] = value[:200]
            else:
                item[key] = value
        parsed.append(item)
    return parsed


def _constituency_lookup(event):
    if not event.organization_id:
        return {}
    lookup = {}
    for constituency in Constituency.objects.filter(organization_id=event.organization_id):
        lookup[constituency.code.lower()] = constituency
        lookup[constituency.name.lower()] = constituency
    return lookup


def _check_plan_limit(event, additional):
    if not event.organization_id:
        return
    from billing.service import check_limit

    check_limit(event.organization, 'max_voters_per_election', event.voters.count() + additional)


def import_voters(event, rows, actor, *, source=Voter.Source.CSV, issue_codes=True, request=None):
    check_perm(actor, 'voter.import', event)
    lifecycle.require_editable(event, 'voters', actor, request)
    _check_plan_limit(event, len(rows))
    report = ImportReport()
    constituencies = _constituency_lookup(event)
    needs_code = 'CODE' in (event.auth_methods or [])
    with transaction.atomic():
        existing = {v.identifier: v for v in event.voters.exclude(identifier__isnull=True)}
        for number, row in enumerate(rows, start=2):
            identifier = (row.get('identifier') or '').strip().upper() or None
            email = (row.get('email') or '').strip() or None
            if identifier and not IDENTIFIER_RE.match(identifier):
                report.errors.append({'row': number, 'error': 'Invalid voter ID characters.'})
                continue
            if email:
                try:
                    validate_email(email)
                except ValidationError:
                    report.errors.append({'row': number, 'error': f'Invalid email "{email[:60]}".'})
                    continue
            if not identifier and not email:
                report.errors.append({'row': number, 'error': 'A voter ID or email is required.'})
                continue
            constituency = None
            if row.get('constituency'):
                constituency = constituencies.get(row['constituency'].strip().lower())
                if constituency is None:
                    report.errors.append({'row': number, 'error': f'Unknown constituency "{row["constituency"][:60]}".'})
                    continue
            voter = existing.get(identifier) if identifier else None
            if voter is None and not identifier and email:
                voter = event.voters.filter(email_index=crypto.blind_index(email, 'email')).first()
            if voter is not None:
                if voter.status == Voter.Status.VOTED:
                    report.skipped += 1
                    continue
                voter.full_name = row.get('full_name') or voter.full_name
                if email:
                    voter.set_email(email)
                if row.get('phone'):
                    voter.set_phone(row['phone'])
                voter.constituency = constituency or voter.constituency
                voter.attributes = {**(voter.attributes or {}), **row.get('attributes', {})}
                voter.save()
                report.updated += 1
                continue
            voter = Voter(election=event, identifier=identifier, full_name=row.get('full_name', ''),
                          constituency=constituency, attributes=row.get('attributes', {}), source=source)
            voter.set_email(email)
            if row.get('phone'):
                voter.set_phone(row['phone'])
            if issue_codes and needs_code:
                voter.issue_credential()
                report.codes_issued += 1
            try:
                with transaction.atomic():
                    voter.save()
            except IntegrityError:
                report.errors.append({'row': number, 'error': 'Duplicate voter ID or credential collision.'})
                continue
            if identifier:
                existing[identifier] = voter
            report.created += 1
        audit.record('VOTERS_IMPORTED', request=request, actor=actor, event=event,
                     summary=f'Voter import: {report.created} created, {report.updated} updated, '
                             f'{report.skipped} skipped, {len(report.errors)} errors',
                     metadata={k: v for k, v in report.as_dict().items() if k != 'errors'})
    if report.created and event.organization_id:
        from billing.service import record_usage

        record_usage(event.organization, 'VOTERS_IMPORTED', report.created, event=event)
    return report


def generate_anonymous_codes(event, count, actor, request=None):
    """Access codes not tied to any identity ("scratch card" mode)."""
    check_perm(actor, 'voter.credentials', event)
    lifecycle.require_editable(event, 'voters', actor, request)
    count = max(0, min(int(count), 5000))
    _check_plan_limit(event, count)
    created = 0
    with transaction.atomic():
        for _ in range(count):
            for _attempt in range(5):
                voter = Voter(election=event, source=Voter.Source.CODES)
                voter.issue_credential()
                try:
                    with transaction.atomic():
                        voter.save()
                    created += 1
                    break
                except IntegrityError:
                    continue
        audit.record('VOTER_CODES_GENERATED', request=request, actor=actor, event=event,
                     summary=f'{created} anonymous access codes generated')
    return created


def add_voter(event, actor, *, identifier=None, full_name='', email=None, phone=None, constituency=None,
              attributes=None, source=Voter.Source.MANUAL, request=None, issue_code=True):
    rows = [{'identifier': identifier, 'full_name': full_name, 'email': email, 'phone': phone,
             'attributes': attributes or {}, 'constituency': constituency.code if constituency else None}]
    report = import_voters(event, rows, actor, source=source, issue_codes=issue_code, request=request)
    if report.errors:
        raise VoterImportError(report.errors[0]['error'])
    if identifier:
        return event.voters.get(identifier=identifier.strip().upper())
    return event.voters.order_by('-pk').first()


def reset_credential(voter, actor, request=None, notify_voter=False):
    event = voter.election
    check_perm(actor, 'voter.credentials', event)
    if voter.status == Voter.Status.VOTED:
        raise VoterImportError('This voter has already voted; their credential cannot be reset.')
    with transaction.atomic():
        voter = Voter.objects.select_for_update().get(pk=voter.pk)
        VoteAuthorization.objects.filter(voter=voter, status=VoteAuthorization.Status.ISSUED) \
            .update(status=VoteAuthorization.Status.REVOKED)
        code = voter.issue_credential()
        voter.save()
        audit.record('VOTER_CREDENTIAL_RESET', request=request, actor=actor, event=event, target=voter,
                     summary=f'Access code reset for voter {voter.identifier or voter.pk}')
    if notify_voter:
        send_invitations(event, actor, Voter.objects.filter(pk=voter.pk), codes={voter.pk: code})
    return code


def reset_all_unused_credentials(event, actor):
    count = 0
    for voter in event.voters.exclude(status=Voter.Status.VOTED).exclude(credential_hash__isnull=True):
        with transaction.atomic():
            locked = Voter.objects.select_for_update().get(pk=voter.pk)
            VoteAuthorization.objects.filter(voter=locked, status=VoteAuthorization.Status.ISSUED) \
                .update(status=VoteAuthorization.Status.REVOKED)
            locked.issue_credential()
            locked.save()
            count += 1
    audit.record('VOTER_CREDENTIALS_BULK_RESET', actor=actor, event=event, summary=f'{count} unused credentials reset')
    return count


def set_status(voter, status, actor, reason='', request=None):
    event = voter.election
    check_perm(actor, 'voter.edit', event)
    if voter.status == Voter.Status.VOTED:
        raise VoterImportError('A voter who has already voted cannot change status.')
    if status not in (Voter.Status.ELIGIBLE, Voter.Status.VERIFIED, Voter.Status.SUSPENDED, Voter.Status.INELIGIBLE):
        raise VoterImportError('Invalid status.')
    old = voter.status
    voter.status = status
    voter.status_reason = reason[:255]
    if status == Voter.Status.VERIFIED and not voter.verified_at:
        voter.verified_at = timezone.now()
    voter.save(update_fields=['status', 'status_reason', 'verified_at', 'updated_at'])
    if status in (Voter.Status.SUSPENDED, Voter.Status.INELIGIBLE):
        VoteAuthorization.objects.filter(voter=voter, status=VoteAuthorization.Status.ISSUED) \
            .update(status=VoteAuthorization.Status.REVOKED)
    audit.record('VOTER_STATUS_CHANGED', request=request, actor=actor, event=event, target=voter, reason=reason,
                 changes={'status': {'old': old, 'new': status}})
    return voter


def remove_voter(voter, actor, request=None):
    event = voter.election
    check_perm(actor, 'voter.edit', event)
    lifecycle.require_editable(event, 'voters', actor, request)
    if voter.status == Voter.Status.VOTED or voter.authorizations.filter(status='CONSUMED').exists():
        raise VoterImportError('Voters who have voted cannot be removed.')
    audit.record('VOTER_REMOVED', request=request, actor=actor, event=event, target=voter,
                 summary=f'Voter {voter.identifier or voter.pk} removed from roll')
    voter.authorizations.all().delete()
    voter.delete()


def clear_unvoted(event, actor, request=None):
    check_perm(actor, 'voter.credentials', event)
    lifecycle.require_editable(event, 'voters', actor, request)
    queryset = event.voters.exclude(status=Voter.Status.VOTED)
    VoteAuthorization.objects.filter(voter__in=queryset).exclude(status='CONSUMED').delete()
    count = queryset.count()
    queryset.delete()
    audit.record('VOTERS_CLEARED', request=request, actor=actor, event=event, summary=f'{count} unvoted voters removed')
    return count


def export_credentials(event, actor, request=None):
    """Rows for distributing access codes (only unused credentials)."""
    check_perm(actor, 'voter.credentials', event)
    rows = []
    for voter in event.voters.order_by('identifier', 'pk'):
        rows.append({
            'identifier': voter.identifier or '', 'name': voter.full_name or '', 'email': voter.email or '',
            'constituency': voter.constituency.code if voter.constituency_id else '',
            'code': voter.credential_ciphertext or '', 'status': voter.status,
            'voted_at': voter.voted_at.isoformat() if voter.voted_at else '',
        })
    audit.record('VOTER_CREDENTIALS_EXPORTED', request=request, actor=actor, event=event,
                 summary=f'Voter roll with {sum(1 for r in rows if r["code"])} unused access codes exported')
    return rows


def vote_link(event):
    return f'{settings.SITE_URL}/e/{event.pk}/vote/'


def send_invitations(event, actor, voters=None, codes=None, request=None):
    from notifications.models import Notification
    from notifications.service import notify

    check_perm(actor, 'voter.credentials', event)
    voters = voters if voters is not None else event.voters.exclude(status=Voter.Status.VOTED)
    sent = 0
    for voter in voters:
        code = (codes or {}).get(voter.pk) or voter.credential_ciphertext
        context = {'name': voter.full_name, 'identifier': voter.identifier or '', 'code': code or '',
                   'starts': timezone.localtime(event.start_date).strftime('%d %b %Y %H:%M'),
                   'ends': timezone.localtime(event.end_date).strftime('%d %b %Y %H:%M %Z'), 'link': vote_link(event)}
        if voter.email:
            notify('voter_invitation', channel=Notification.Channel.EMAIL, recipient=voter.email, context=context, event=event)
            sent += 1
        elif voter.phone:
            notify('voter_invitation', channel=Notification.Channel.SMS, recipient=voter.phone, context=context, event=event)
            sent += 1
        Voter.objects.filter(pk=voter.pk).update(invited_at=timezone.now())
    audit.record('VOTER_INVITATIONS_SENT', request=request, actor=actor, event=event, summary=f'{sent} invitations queued')
    return sent


def resend_credential_public(event, identifier, request=None):
    """Self-service "lost my code": a NEW code goes ONLY to the email on the
    roll (never to an address typed into the form). Same response either way."""
    identifier = (identifier or '').strip().upper()
    if not identifier:
        return
    with transaction.atomic():
        voter = Voter.objects.select_for_update().filter(election=event, identifier=identifier).first()
        if voter is None or voter.status not in (Voter.Status.ELIGIBLE, Voter.Status.VERIFIED):
            audit.record('VOTER_CODE_RESEND_REQUESTED', request=request, event=event, result='FAILURE',
                         summary='Code resend requested for unknown/ineligible voter ID')
            return
        if not voter.email or 'CODE' not in (event.auth_methods or []):
            audit.record('VOTER_CODE_RESEND_REQUESTED', request=request, event=event, target=voter,
                         result='FAILURE', summary='Code resend requested but no email on file')
            return
        VoteAuthorization.objects.filter(voter=voter, status=VoteAuthorization.Status.ISSUED) \
            .update(status=VoteAuthorization.Status.REVOKED)
        code = voter.issue_credential()
        voter.save()
        audit.record('VOTER_CODE_RESENT', request=request, event=event, target=voter,
                     summary=f'Access code reset and emailed to roll address for {voter.identifier}')
    from notifications.models import Notification
    from notifications.service import notify

    notify('voter_invitation', channel=Notification.Channel.EMAIL, recipient=voter.email, event=event, immediate=True,
           context={'name': voter.full_name, 'identifier': voter.identifier, 'code': code, 'link': vote_link(event),
                    'starts': timezone.localtime(event.start_date).strftime('%d %b %Y %H:%M'),
                    'ends': timezone.localtime(event.end_date).strftime('%d %b %Y %H:%M %Z')})


def self_register(event, *, identifier, email, full_name='', phone='', request=None):
    """Create a VERIFIED voter after the caller has verified the email by OTP."""
    if not event.allow_self_registration:
        raise VoterImportError('Self-registration is not enabled for this election.')
    if event.voter_list_frozen or event.status in lifecycle.LOCKED_STATES:
        raise VoterImportError('Registration for this election is closed.')
    domains = event.email_domain_list
    if domains and email.rsplit('@', 1)[-1].lower() not in domains:
        raise VoterImportError('Use your institutional email address to register.')
    identifier = (identifier or '').strip().upper() or None
    if identifier and not IDENTIFIER_RE.match(identifier):
        raise VoterImportError('Invalid ID.')
    if event.voters.filter(email_index=crypto.blind_index(email, 'email')).exists() or \
            (identifier and event.voters.filter(identifier=identifier).exists()):
        raise VoterImportError('You are already registered for this election.')
    _check_plan_limit(event, 1)
    now = timezone.now()
    voter = Voter(election=event, identifier=identifier, full_name=full_name, source=Voter.Source.SELF_REGISTRATION,
                  status=Voter.Status.VERIFIED, email_verified_at=now, verified_at=now)
    voter.set_email(email)
    if phone:
        voter.set_phone(phone)
    code = voter.issue_credential() if 'CODE' in (event.auth_methods or []) else None
    voter.save()
    audit.record('VOTER_SELF_REGISTERED', request=request, event=event, target=voter,
                 summary=f'Voter {identifier or voter.pk} self-registered (email verified)')
    from notifications.models import Notification
    from notifications.service import notify

    notify('voter_registration', channel=Notification.Channel.EMAIL, recipient=email, event=event,
           context={'name': full_name, 'code': code or '', 'link': vote_link(event)})
    return voter


def turnout(event):
    counts = dict(event.voters.values_list('status').annotate(n=Count('pk')))
    eligible = sum(n for status, n in counts.items() if status not in (Voter.Status.SUSPENDED, Voter.Status.INELIGIBLE))
    voted = counts.get(Voter.Status.VOTED, 0)
    by_constituency = []
    rows = event.voters.values('constituency__name').annotate(total=Count('pk')).order_by('constituency__name')
    for row in rows:
        voted_here = event.voters.filter(constituency__name=row['constituency__name'], status=Voter.Status.VOTED).count()
        by_constituency.append({'name': row['constituency__name'] or 'Unassigned', 'total': row['total'],
                                'voted': voted_here,
                                'turnout': round(100.0 * voted_here / row['total'], 1) if row['total'] else 0.0})
    return {
        'statuses': counts, 'eligible': eligible, 'voted': voted, 'total': sum(counts.values()),
        'turnout_percentage': round(100.0 * voted / eligible, 2) if eligible else 0.0,
        'by_constituency': by_constituency,
    }


def credential_matches(event, voter, code):
    return crypto.constant_time_equals(voter.credential_hash or '', hash_voting_code(event.pk, code))
