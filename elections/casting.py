"""Secret-ballot casting.

    Voter identity --(eligibility)--> VoteAuthorization(token hash)
    --(anonymous token)--> encrypted Ballot --> tally

What is persisted:
  * Voter: status VOTED + voted_at  ("this person was eligible and voted")
  * VoteAuthorization: voter + hash(token), CONSUMED
  * Ballot: random UUID, ciphertext, tracker - no voter, no token, no time
so the database cannot answer "how did this person vote?".
"""
import json
import time
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from core import audit, crypto, metrics
from voting.models import Event

from .ballot import BallotError, validate_ballot
from .eligibility import ballot_style, election_eligibility
from .models import Ballot, VoteAuthorization, Voter

AUTHORIZATION_TTL = timedelta(minutes=30)


class CastError(Exception):
    def __init__(self, message, code='invalid'):
        super().__init__(message)
        self.message = message
        self.code = code


class AlreadyVoted(CastError):
    def __init__(self):
        super().__init__('A ballot has already been recorded for this voter.', 'already_voted')


def token_hash(token):
    return crypto.keyed_hash('ballot-token', token)


def style_hash(event, style):
    return crypto.sha256_hex(crypto.canonical_json({'election': event.pk, 'positions': sorted(style)}))


def ensure_accepting(event):
    if not event.accepting_votes():
        if event.status == Event.Status.PAUSED:
            raise CastError('Voting is temporarily paused. Please try again later.', 'paused')
        if event.status in (Event.Status.SCHEDULED, Event.Status.APPROVED) or timezone.now() < event.start_date:
            raise CastError('Voting has not opened yet.', 'not_open')
        raise CastError('Voting for this election has closed.', 'closed')


def issue_authorization(event, voter, method, request=None):
    """Return (token, authorization). Any earlier unused authorization for the
    voter is revoked, so a lost/duplicated token can never add a vote."""
    ensure_accepting(event)
    with transaction.atomic():
        voter = Voter.objects.select_for_update().get(pk=voter.pk)
        if voter.status == Voter.Status.VOTED:
            raise AlreadyVoted()
        eligible, reasons = election_eligibility(event, voter)
        if not eligible:
            raise CastError(' '.join(reasons), 'ineligible')
        style = ballot_style(event, voter)
        if not style:
            raise CastError('There are no positions on this ballot that you are eligible to vote for.', 'no_positions')
        VoteAuthorization.objects.filter(voter=voter, status=VoteAuthorization.Status.ISSUED) \
            .update(status=VoteAuthorization.Status.REVOKED)
        token = crypto.random_token(32)
        now = timezone.now()
        authorization = VoteAuthorization.objects.create(
            election=event, voter=voter, token_hash=token_hash(token), auth_method=method, ballot_style=style,
            issued_at=now, expires_at=min(now + AUTHORIZATION_TTL, event.end_date),
        )
        audit.record('VOTER_BALLOT_ISSUED', request=request, event=event, target_type='elections.Voter',
                     target_id=str(voter.pk), summary=f'Ballot issued to voter {voter.identifier or voter.pk}',
                     metadata={'method': method, 'positions': len(style)})
    return token, authorization


def authorization_for(token):
    if not token:
        return None
    return VoteAuthorization.objects.filter(token_hash=token_hash(token)).select_related('election').first()


def cast_ballot(event, token, payload, request=None):
    """Validate, encrypt and record one ballot. Returns a receipt with the
    ballot tracker (proves inclusion, reveals nothing about the choices)."""
    started = time.perf_counter()
    try:
        receipt = _cast(event, token, payload, request)
    except CastError as exc:
        metrics.VOTE_SUBMISSIONS.labels(mode='ballot', outcome=exc.code).inc()
        raise
    except BallotError:
        metrics.VOTE_SUBMISSIONS.labels(mode='ballot', outcome='invalid_ballot').inc()
        raise
    metrics.VOTE_SUBMISSIONS.labels(mode='ballot', outcome='success').inc()
    metrics.VOTES.labels(mode='ballot').inc()
    metrics.VOTE_LATENCY.labels(mode='ballot').observe(time.perf_counter() - started)
    return receipt


def _cast(event, token, payload, request):
    with transaction.atomic():
        # Lock the authorization first: concurrent submissions of the same
        # token serialize here and all but the first see CONSUMED.
        authorization = VoteAuthorization.objects.select_for_update().filter(
            token_hash=token_hash(token or ''), election=event).first()
        if authorization is None:
            raise CastError('Your ballot session is invalid. Please sign in again.', 'invalid_token')
        if authorization.status == VoteAuthorization.Status.CONSUMED:
            raise AlreadyVoted()
        if authorization.status != VoteAuthorization.Status.ISSUED or authorization.expires_at <= timezone.now():
            raise CastError('Your ballot session has expired. Please sign in again.', 'expired')
        event = Event.objects.get(pk=event.pk)
        ensure_accepting(event)
        voter = Voter.objects.select_for_update().get(pk=authorization.voter_id)
        if voter.status == Voter.Status.VOTED:
            raise AlreadyVoted()
        if not voter.can_vote:
            raise CastError('Your voter record cannot vote. Contact the election officials.', 'ineligible')

        style = authorization.ballot_style
        selections = validate_ballot(event, style, payload)
        s_hash = style_hash(event, style)
        plaintext = crypto.canonical_json({
            'v': 1, 'election': event.pk, 'style': sorted(style), 'selections': selections,
            # Random padding makes equal ballots produce unrelated ciphertexts
            # and trackers.
            'nonce': crypto.random_token(16),
        })
        key = event.ballot_key
        ciphertext = crypto.seal(key.public_key, plaintext, aad=f'{event.pk}:{s_hash}'.encode())
        tracker = crypto.sha256_hex(ciphertext)
        Ballot.objects.create(
            election=event, ciphertext=ciphertext, tracker=tracker, style_hash=s_hash,
            constituency_id=voter.constituency_id if event.record_constituency_on_ballot else None,
        )
        now = timezone.now()
        VoteAuthorization.objects.filter(pk=authorization.pk).update(
            status=VoteAuthorization.Status.CONSUMED, consumed_at=now)
        voter.status = Voter.Status.VOTED
        voter.voted_at = now
        voter.wipe_credential_plaintext()
        voter.save(update_fields=['status', 'voted_at', 'credential_ciphertext', 'updated_at'])
        # Participation only - the tracker is deliberately NOT logged here.
        audit.record('VOTER_VOTED', request=request, event=event, target_type='elections.Voter',
                     target_id=str(voter.pk), summary=f'Voter {voter.identifier or voter.pk} cast a ballot')
        transaction.on_commit(lambda: _after_cast(event.pk, voter.pk))
    return {'tracker': tracker, 'cast_at': now.isoformat(), 'election': event.title, 'positions': len(style)}


def _after_cast(event_id, voter_id):
    from . import tasks

    tasks.send_vote_confirmation.delay(event_id, voter_id)


def selections_from_post(event, style, post):
    """Translate an HTML form POST into the ballot payload shape."""
    from voting.models import Category

    payload = {}
    for position in event.categories.filter(pk__in=style):
        key = f'pos_{position.pk}'
        if post.get(f'{key}_abstain') == 'on':
            payload[str(position.pk)] = None
            continue
        bt = position.ballot_type
        if bt == Category.BallotType.SCORE:
            scores = {}
            for name, value in post.items():
                if name.startswith(f'{key}_score_') and value != '':
                    scores[name.rsplit('_', 1)[-1]] = value
            payload[str(position.pk)] = scores
        elif bt == Category.BallotType.RANKED:
            ranks = []
            for name, value in post.items():
                if name.startswith(f'{key}_rank_') and value not in ('', None):
                    try:
                        ranks.append((int(value), name.rsplit('_', 1)[-1]))
                    except ValueError:
                        raise BallotError(f'Invalid rank for "{position.name}".', position) from None
            ranks.sort()
            numbers = [r for r, _ in ranks]
            if numbers != list(range(1, len(numbers) + 1)):
                raise BallotError(f'Ranks for "{position.name}" must be 1, 2, 3... with no gaps or repeats.', position)
            payload[str(position.pk)] = [cid for _, cid in ranks]
        elif bt == Category.BallotType.REFERENDUM:
            payload[str(position.pk)] = post.get(key, '')
        else:
            payload[str(position.pk)] = post.getlist(key)
    return payload


def payload_json(payload):
    return json.dumps(payload)
