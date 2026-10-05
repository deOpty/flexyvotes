"""Tallying, results review, certification, publication and verification."""
import json
import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models import IntegerField, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from core import audit, crypto
from core.rbac import check_perm
from voting.models import Candidate, Event, VoteTransaction

from . import lifecycle
from .ballot import active_candidates, positions_for
from .eligibility import eligible_voter_count
from .keys import private_key_for_tally, wipe_submitted_shares
from .models import Ballot, ElectionResult, Recount, ResultCertification, Voter

logger = logging.getLogger(__name__)

VOLATILE_KEYS = ('generated_at',)


class ResultsError(Exception):
    pass


def result_hash(data):
    stable = {k: v for k, v in data.items() if k not in VOLATILE_KEYS}
    return crypto.sha256_hex(crypto.canonical_json(stable))


def _position_specs(event):
    specs = []
    for position in positions_for(event):
        specs.append({
            'id': position.pk, 'name': position.name, 'ballot_type': position.ballot_type,
            'seats': position.seats, 'max_score': position.max_score,
            'referendum_threshold': float(position.referendum_threshold),
            'candidates': [{'id': c.pk, 'name': c.name} for c in (
                [] if position.is_referendum else active_candidates(position))],
            'constituency_id': position.constituency_id,
        })
    return specs


def _decrypt_ballots(event, private_key):
    """Yield (selections, constituency_id, tracker) for valid ballots and
    count invalid ones (failed decryption/authentication or malformed)."""
    invalid = []
    valid = []
    for ballot in Ballot.objects.filter(election=event).order_by('tracker').iterator():
        try:
            plaintext = crypto.unseal(private_key, ballot.ciphertext, aad=f'{event.pk}:{ballot.style_hash}'.encode())
            data = json.loads(plaintext)
            if data.get('election') != event.pk or not isinstance(data.get('selections'), dict):
                raise ValueError('ballot belongs to another election')
            valid.append((data['selections'], ballot.constituency_id, ballot.tracker))
        except Exception:  # noqa: BLE001 - any failure makes the ballot invalid, never crashes the count
            invalid.append(ballot.tracker)
    return valid, invalid


def _legacy_counts(event):
    """Votes recorded by the pre-platform code-voting flow (cleartext ledger)."""
    return dict(VoteTransaction.objects.filter(candidate__event=event, status=VoteTransaction.Status.SUCCESS)
                .values_list('candidate_id').annotate(total=Sum('number_of_votes')))


def compute_institutional_tally(event, private_key):
    from .tally import tally_position

    specs = _position_specs(event)
    valid, invalid = _decrypt_ballots(event, private_key)
    legacy = _legacy_counts(event)
    positions = []
    constituency_rows = {}
    for spec in specs:
        key = str(spec['id'])
        selections = []
        by_constituency = {}
        for ballot_selections, constituency_id, _ in valid:
            if key not in ballot_selections:
                continue
            value = ballot_selections[key]
            selections.append(value)
            if constituency_id is not None:
                by_constituency.setdefault(constituency_id, []).append(value)
        if legacy and spec['ballot_type'] in ('SINGLE', 'FPTP', 'MULTIPLE', 'APPROVAL'):
            for candidate in spec['candidates']:
                selections.extend([[candidate['id']]] * int(legacy.get(candidate['id'], 0)))
        result = tally_position(spec, selections, seed=str(event.pk))
        result['invalid_ballots'] = len(invalid)
        positions.append(result)
        for constituency_id, values in by_constituency.items():
            constituency_rows.setdefault(constituency_id, []).append((spec, values))

    constituency_results = []
    if event.record_constituency_on_ballot:
        from .models import Constituency
        from .tally import tally_position as tp

        names = dict(Constituency.objects.filter(pk__in=constituency_rows).values_list('pk', 'name'))
        for constituency_id, items in sorted(constituency_rows.items()):
            ballots_here = max((len(values) for _, values in items), default=0)
            entry = {'constituency_id': constituency_id, 'name': names.get(constituency_id, '?'),
                     'ballots': ballots_here}
            if ballots_here < event.min_anonymity_set:
                # Too few ballots to publish without risking identification.
                entry['suppressed'] = True
            else:
                entry['positions'] = [tp(spec, values, seed=str(event.pk)) for spec, values in items]
            constituency_results.append(entry)

    trackers = sorted(t for _, _, t in valid) + sorted(invalid)
    trackers.sort()
    eligible = eligible_voter_count(event)
    participated = event.voters.filter(status=Voter.Status.VOTED).count()
    return {
        'election': {'id': event.pk, 'title': event.title, 'mode': 'institutional',
                     'organization': event.organization.name if event.organization_id else ''},
        'positions': positions,
        'constituencies': constituency_results,
        'turnout': {
            'eligible_voters': eligible, 'votes_cast': participated,
            'turnout_percentage': round(100.0 * participated / eligible, 2) if eligible else 0.0,
            'ballots_counted': len(valid), 'invalid_ballots': len(invalid),
            'legacy_votes': sum(int(v) for v in legacy.values()),
        },
        'bulletin': {'ballots': len(trackers), 'root': crypto.merkle_root(trackers)},
        'generated_at': timezone.now().isoformat(),
    }


def paid_counts(event):
    rows = Candidate.objects.filter(event=event).annotate(
        main=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Main')), 0, output_field=IntegerField()),
        tie=Coalesce(Sum('transactions__number_of_votes', filter=Q(transactions__status='Success', transactions__vote_type='Tie-Breaker')), 0, output_field=IntegerField()),
    ).values('pk', 'name', 'category_id', 'category__name', 'main', 'tie', 'status')
    return list(rows)


def compute_paid_tally(event):
    rows = paid_counts(event)
    groups = {}
    for row in rows:
        groups.setdefault(row['category_id'], {'name': row['category__name'] or 'All contestants', 'rows': []})['rows'].append(row)
    positions = []
    for category_id, group in groups.items():
        total = sum(r['main'] for r in group['rows'])
        ordered = sorted(group['rows'], key=lambda r: (-r['main'], -r['tie'], r['name'].lower()))
        candidates = [{'candidate_id': r['pk'], 'name': r['name'], 'votes': r['main'], 'tie_breaker_votes': r['tie'],
                       'percentage': round(100.0 * r['main'] / total, 2) if total else 0.0, 'rank': i + 1}
                      for i, r in enumerate(ordered)]
        winners = [candidates[0]['candidate_id']] if candidates and candidates[0]['votes'] > 0 else []
        ties = [c['candidate_id'] for c in candidates if candidates and c['votes'] == candidates[0]['votes']
                and c['tie_breaker_votes'] == candidates[0]['tie_breaker_votes']] if candidates else []
        positions.append({'position_id': category_id, 'position': group['name'], 'ballot_type': 'PAID',
                          'method': 'paid-plurality', 'total_votes': total, 'valid_ballots': total, 'abstentions': 0,
                          'candidates': candidates, 'winners': winners, 'ties': ties if len(ties) > 1 else []})
    revenue = VoteTransaction.objects.filter(candidate__event=event, status='Success').aggregate(total=Sum('amount'))['total'] or 0
    total_votes = sum(p['total_votes'] for p in positions)
    return {
        'election': {'id': event.pk, 'title': event.title, 'mode': 'paid',
                     'organization': event.organization.name if event.organization_id else ''},
        'positions': positions, 'constituencies': [],
        'turnout': {'votes_cast': total_votes, 'eligible_voters': None, 'turnout_percentage': None,
                    'ballots_counted': total_votes, 'invalid_ballots': 0},
        'revenue': {'amount': str(revenue), 'currency': event.currency},
        'bulletin': {'ballots': 0, 'root': crypto.merkle_root([])},
        'generated_at': timezone.now().isoformat(),
    }


def compute_tally(event):
    if event.is_institutional:
        return compute_institutional_tally(event, private_key_for_tally(event))
    return compute_paid_tally(event)


def run_tally(event, actor=None, request=None, kind=ElectionResult.Kind.OFFICIAL, system=False):
    if kind == ElectionResult.Kind.OFFICIAL:
        if not system:
            check_perm(actor, 'results.tally', event)
        if event.status == Event.Status.CLOSED:
            event = lifecycle.transition(event, 'start_tally', actor=actor, request=request, system=system,
                                         reason='Tally started')
        if event.status != Event.Status.TALLYING:
            raise ResultsError('Results can only be tallied after voting has closed.')
    data = compute_tally(event)
    digest = result_hash(data)
    with transaction.atomic():
        if kind == ElectionResult.Kind.OFFICIAL:
            ElectionResult.objects.filter(election=event, kind=kind, status=ElectionResult.Status.PENDING_REVIEW) \
                .update(status=ElectionResult.Status.SUPERSEDED)
        result = ElectionResult.objects.create(
            election=event, kind=kind,
            status=ElectionResult.Status.PENDING_REVIEW if kind == ElectionResult.Kind.OFFICIAL else ElectionResult.Status.INFORMATIONAL,
            data=json.loads(crypto.canonical_json(data)), result_hash=digest,
            bulletin_root=data['bulletin']['root'], ballots_counted=data['turnout']['ballots_counted'] or 0,
            eligible_voters=data['turnout'].get('eligible_voters') or 0, votes_cast=data['turnout']['votes_cast'] or 0,
            tallied_by=actor,
        )
        audit.record('RESULTS_TALLIED', request=request, actor=actor, event=event, target=result,
                     summary=f'{result.get_kind_display()} computed', metadata={'result_hash': digest,
                                                                                'ballots': result.ballots_counted})
    if event.is_institutional and kind == ElectionResult.Kind.OFFICIAL:
        wipe_submitted_shares(event)
    return result


def _latest_snapshot_hash(event):
    snapshot = event.config_snapshots.order_by('-version').first()
    return snapshot.config_hash if snapshot else ''


def approve_and_certify(result, reviewer, notes='', request=None):
    event = result.election
    check_perm(reviewer, 'results.approve', event)
    check_perm(reviewer, 'results.certify', event)
    if result.kind != ElectionResult.Kind.OFFICIAL or result.status != ElectionResult.Status.PENDING_REVIEW:
        raise ResultsError('Only a pending official result can be certified.')
    if event.status != Event.Status.TALLYING:
        raise ResultsError('The election is not awaiting certification.')
    if event.dual_approval_required and result.tallied_by_id and result.tallied_by_id == reviewer.pk:
        raise ResultsError('Separation of duties: results must be certified by someone other than the person who tallied them.')
    now = timezone.now()
    payload = {
        'platform': 'FlexyVotes', 'election_id': event.pk, 'election_title': event.title,
        'organization': event.organization.name if event.organization_id else '',
        'result_hash': result.result_hash, 'bulletin_root': result.bulletin_root,
        'ballots_counted': result.ballots_counted, 'votes_cast': result.votes_cast,
        'eligible_voters': result.eligible_voters, 'config_hash': _latest_snapshot_hash(event),
        'ballot_key_fingerprint': event.ballot_key.fingerprint if hasattr(event, 'ballot_key') else '',
        'certified_at': now.isoformat(),
    }
    encoded = crypto.canonical_json(payload)
    with transaction.atomic():
        result.status = ElectionResult.Status.APPROVED
        result.reviewed_by = reviewer
        result.reviewed_at = now
        result.review_notes = notes
        result.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_notes'])
        certification = ResultCertification.objects.create(
            election=event, result=result, payload=payload, payload_hash=crypto.sha256_hex(encoded),
            signature=crypto.sign(encoded), public_key=crypto.public_key_b64(),
            key_id=crypto.key_fingerprint(crypto.public_key_b64()), tallied_by=result.tallied_by,
            certified_by=reviewer, certified_at=now,
        )
        lifecycle.transition(event, 'certify', actor=reviewer, request=request, reason=notes or 'Results certified',
                             extra_effect=lambda e: setattr(e, 'certified_at', now))
        audit.record('RESULTS_CERTIFIED', request=request, actor=reviewer, event=event, target=certification,
                     summary='Results approved and certified',
                     metadata={'result_hash': result.result_hash, 'signature': certification.signature})
    return certification


def reject_result(result, reviewer, notes, request=None):
    check_perm(reviewer, 'results.approve', result.election)
    if result.status != ElectionResult.Status.PENDING_REVIEW:
        raise ResultsError('Only pending results can be rejected.')
    result.status = ElectionResult.Status.REJECTED
    result.reviewed_by = reviewer
    result.reviewed_at = timezone.now()
    result.review_notes = notes
    result.save(update_fields=['status', 'reviewed_by', 'reviewed_at', 'review_notes'])
    audit.record('RESULTS_REJECTED', request=request, actor=reviewer, event=result.election, target=result,
                 summary='Tallied results rejected', reason=notes)
    return result


def current_certification(event):
    return event.certifications.filter(revoked_at__isnull=True).select_related('result').first()


def revoke_certification(event, actor, reason):
    certification = current_certification(event)
    if certification is None:
        raise ResultsError('There is no current certification to revoke.')
    with transaction.atomic():
        ResultCertification.objects.filter(pk=certification.pk).update(revoked_at=timezone.now(), revoked_reason=reason)
        ElectionResult.objects.filter(pk=certification.result_id).update(status=ElectionResult.Status.SUPERSEDED)
        lifecycle.transition(event, 'decertify', actor=actor, system=True, reason=reason)
        audit.record('RESULTS_DECERTIFIED', actor=actor, event=event, target=certification, reason=reason,
                     summary='Result certification revoked')


def _differences(official, recount):
    diffs = []
    official_positions = {str(p.get('position_id')): p for p in official.get('positions', [])}
    for position in recount.get('positions', []):
        before = official_positions.get(str(position.get('position_id')))
        if before is None:
            diffs.append({'position': position.get('position'), 'issue': 'missing in official result'})
            continue
        before_votes = {str(c['candidate_id']): c['votes'] for c in before.get('candidates', [])}
        for candidate in position.get('candidates', []):
            old = before_votes.get(str(candidate['candidate_id']))
            if old != candidate['votes']:
                diffs.append({'position': position.get('position'), 'candidate': candidate['name'],
                              'official': old, 'recount': candidate['votes']})
        if sorted(map(str, before.get('winners', []))) != sorted(map(str, position.get('winners', []))):
            diffs.append({'position': position.get('position'), 'issue': 'different winners'})
    return diffs


def recount(event, actor, kind=Recount.Kind.MANUAL, reason='', request=None):
    check_perm(actor, 'results.recount', event)
    if event.status not in (Event.Status.TALLYING, Event.Status.CERTIFIED, Event.Status.PUBLISHED, Event.Status.ARCHIVED):
        raise ResultsError('Recounts are available once tallying has started.')
    official = current_certification(event)
    reference = official.result if official else event.results.filter(
        kind=ElectionResult.Kind.OFFICIAL).exclude(status=ElectionResult.Status.SUPERSEDED).first()
    result_kind = ElectionResult.Kind.INDEPENDENT if kind == Recount.Kind.INDEPENDENT else ElectionResult.Kind.RECOUNT
    result = run_tally(event, actor=actor, request=request, kind=result_kind)
    diffs = _differences(reference.data, result.data) if reference else []
    matches = reference is not None and reference.result_hash == result.result_hash
    record = Recount.objects.create(election=event, kind=kind, reason=reason, requested_by=actor, result=result,
                                    compared_to=reference, matches=matches if reference else None, differences=diffs)
    audit.record('RESULTS_RECOUNT', request=request, actor=actor, event=event, target=record, reason=reason,
                 summary=f'{record.get_kind_display()}: {"matches" if matches else "DIFFERS FROM"} official result',
                 metadata={'matches': matches, 'differences': len(diffs), 'result_hash': result.result_hash})
    return record


def live_results(event):
    """Cached live counts for paid elections (never for secret ballots)."""
    key = f'fv:live:{event.pk}'
    data = cache.get(key)
    if data is None:
        data = compute_paid_tally(event)
        cache.set(key, data, 5)
    return data


def public_results(event):
    """What the public may see right now, or None."""
    if event.status in (Event.Status.PUBLISHED, Event.Status.ARCHIVED):
        certification = current_certification(event)
        if certification:
            return {'certified': True, 'data': certification.result.data, 'certification': certification}
    if event.is_paid and event.results_are_public:
        return {'certified': False, 'data': live_results(event), 'certification': None}
    return None


def bulletin_trackers(event):
    return list(Ballot.objects.filter(election=event).order_by('tracker').values_list('tracker', flat=True))


def tracker_proof(event, tracker):
    trackers = bulletin_trackers(event)
    tracker = (tracker or '').strip().lower()
    if tracker not in trackers:
        return {'included': False, 'root': crypto.merkle_root(trackers), 'ballots': len(trackers)}
    index = trackers.index(tracker)
    return {'included': True, 'index': index, 'proof': crypto.merkle_proof(trackers, index),
            'root': crypto.merkle_root(trackers), 'ballots': len(trackers)}


def verification_bundle(event):
    """Everything an observer needs to independently check the published
    result: signed configuration, signed certification, bulletin board."""
    certification = current_certification(event)
    snapshot = event.config_snapshots.order_by('-version').first()
    trackers = bulletin_trackers(event)
    return {
        'format': 'flexyvotes-verification-v1',
        'election': {'id': event.pk, 'title': event.title, 'status': event.status,
                     'organization': event.organization.name if event.organization_id else ''},
        'configuration': {
            'version': snapshot.version, 'config': snapshot.config, 'config_hash': snapshot.config_hash,
            'signature': snapshot.signature, 'public_key': snapshot.public_key,
        } if snapshot else None,
        'certification': {
            'payload': certification.payload, 'payload_hash': certification.payload_hash,
            'signature': certification.signature, 'public_key': certification.public_key,
            'key_id': certification.key_id,
        } if certification else None,
        'result': certification.result.data if certification and event.status in (
            Event.Status.PUBLISHED, Event.Status.ARCHIVED) else None,
        'bulletin_board': {'trackers': trackers, 'merkle_root': crypto.merkle_root(trackers)},
        'verification_keys': crypto.trusted_public_keys(),
    }


def verify_bundle(bundle):
    """Independent checks over a verification bundle. Returns list of (check, ok, detail)."""
    checks = []
    configuration = bundle.get('configuration')
    if configuration:
        ok_hash = crypto.sha256_hex(crypto.canonical_json(configuration['config'])) == configuration['config_hash']
        ok_sig = crypto.verify_signature(crypto.canonical_json(configuration['config']), configuration['signature'],
                                         configuration['public_key'])
        checks.append(('Configuration hash', ok_hash, configuration['config_hash']))
        checks.append(('Configuration signature', ok_sig, configuration['public_key'][:16] + '…'))
    certification = bundle.get('certification')
    if certification:
        encoded = crypto.canonical_json(certification['payload'])
        checks.append(('Certification signature',
                       crypto.verify_signature(encoded, certification['signature'], certification['public_key']),
                       certification['key_id']))
        root = crypto.merkle_root(bundle['bulletin_board']['trackers'])
        checks.append(('Bulletin board Merkle root', root == certification['payload'].get('bulletin_root'), root))
        if bundle.get('result') is not None:
            checks.append(('Result hash', result_hash(bundle['result']) == certification['payload'].get('result_hash'),
                           certification['payload'].get('result_hash')))
        if configuration:
            checks.append(('Certified configuration', certification['payload'].get('config_hash') == configuration['config_hash'],
                           configuration['config_hash']))
        checks.append(('Signing key trusted', certification['public_key'] in bundle.get('verification_keys', []),
                       certification['key_id']))
    return checks
