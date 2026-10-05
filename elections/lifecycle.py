"""Explicit election state machine.

    DRAFT -> REVIEW -> APPROVED -> SCHEDULED -> OPEN <-> PAUSED -> CLOSED
          -> TALLYING -> CERTIFIED -> PUBLISHED -> ARCHIVED

Every transition is permission-checked, guarded by preconditions, applied
under a row lock, and written to the audit log. Which edits are allowed in
each state is answered by :func:`edit_policy`.
"""
from dataclasses import dataclass, field
from typing import Callable

from django.db import transaction
from django.utils import timezone

from core import audit
from core.rbac import check_perm
from voting.models import Event

S = Event.Status


class LifecycleError(Exception):
    pass


@dataclass(frozen=True)
class Action:
    name: str
    sources: tuple
    target: str
    perm: str
    label: str
    style: str = 'outline-secondary'
    confirm: str = ''
    guard: Callable = field(default=None)
    effect: Callable = field(default=None)
    manual: bool = True


def _guard_submit(event, actor):
    from .ballot import configuration_problems

    problems = configuration_problems(event)
    if problems:
        raise LifecycleError('Fix these before submitting: ' + '; '.join(problems))


def _effect_submit(event, actor):
    event.submitted_by = actor


def _guard_approve(event, actor):
    if event.dual_approval_required and actor is not None and event.submitted_by_id == actor.pk:
        raise LifecycleError('Separation of duties: the person who submitted the election cannot approve it.')


def _effect_approve(event, actor):
    from .integrity import snapshot_configuration

    event.reviewed_by = actor
    snapshot_configuration(event, actor, reason='APPROVED')


def _guard_schedule(event, actor):
    from .ballot import configuration_problems

    problems = configuration_problems(event)
    if problems:
        raise LifecycleError('; '.join(problems))
    if event.end_date <= timezone.now():
        raise LifecycleError('The voting end date is in the past.')


def _effect_schedule(event, actor):
    from .keys import ensure_election_key

    if event.is_institutional:
        ensure_election_key(event, actor)


def _guard_open(event, actor):
    if timezone.now() >= event.end_date:
        raise LifecycleError('The voting end date has already passed.')
    if event.is_institutional and not hasattr(event, 'ballot_key'):
        from .keys import ensure_election_key

        ensure_election_key(event, actor)


def _effect_open(event, actor):
    now = timezone.now()
    if event.start_date > now:
        event.start_date = now
    event.opened_at = event.opened_at or now
    if event.is_institutional:
        event.config_frozen = True
        event.ballot_frozen = True
        event.candidates_frozen = True
        if not event.allow_self_registration:
            event.voter_list_frozen = True


def _guard_resume(event, actor):
    if timezone.now() >= event.end_date:
        raise LifecycleError('Voting period has ended; it cannot be resumed.')


def _effect_close(event, actor):
    now = timezone.now()
    event.closed_at = now
    if event.end_date > now:
        event.end_date = now
    event.config_frozen = event.ballot_frozen = event.candidates_frozen = event.voter_list_frozen = True


def _guard_archive(event, actor):
    if event.legal_hold:
        raise LifecycleError('This election is under legal hold and cannot be archived.')
    if event.disputes.filter(status__in=['OPEN', 'UNDER_REVIEW']).exists():
        raise LifecycleError('Resolve open disputes before archiving.')


def _effect_archive(event, actor):
    event.archived_at = timezone.now()
    event.is_active = False


def _effect_publish(event, actor):
    event.published_at = timezone.now()


def _effect_withdraw(event, actor):
    event.submitted_by = None


ACTIONS = {a.name: a for a in [
    Action('submit', (S.DRAFT,), S.REVIEW, 'election.submit', 'Submit for review', 'warning',
           guard=_guard_submit, effect=_effect_submit),
    Action('withdraw', (S.REVIEW,), S.DRAFT, 'election.submit', 'Withdraw from review', effect=_effect_withdraw),
    Action('approve', (S.REVIEW,), S.APPROVED, 'election.review', 'Approve configuration', 'success',
           guard=_guard_approve, effect=_effect_approve),
    Action('reject', (S.REVIEW,), S.DRAFT, 'election.review', 'Return to draft', 'outline-danger'),
    Action('schedule', (S.APPROVED,), S.SCHEDULED, 'election.publish', 'Publish & schedule', 'primary',
           guard=_guard_schedule, effect=_effect_schedule),
    Action('unschedule', (S.SCHEDULED,), S.APPROVED, 'election.publish', 'Unschedule'),
    Action('open', (S.SCHEDULED,), S.OPEN, 'election.publish', 'Open voting now', 'success',
           confirm='Open voting immediately?', guard=_guard_open, effect=_effect_open),
    Action('pause', (S.OPEN,), S.PAUSED, 'election.pause', 'Pause voting', 'warning',
           confirm='Pause voting? Voters will not be able to cast ballots until resumed.'),
    Action('resume', (S.PAUSED,), S.OPEN, 'election.pause', 'Resume voting', 'success', guard=_guard_resume),
    Action('close', (S.OPEN, S.PAUSED), S.CLOSED, 'election.close', 'Close voting', 'danger',
           confirm='Close voting now? This cannot be undone without dual approval.', effect=_effect_close),
    Action('start_tally', (S.CLOSED,), S.TALLYING, 'results.tally', 'Start tally', 'primary'),
    Action('certify', (S.TALLYING,), S.CERTIFIED, 'results.certify', 'Certify', 'success', manual=False),
    Action('publish', (S.CERTIFIED,), S.PUBLISHED, 'results.publish', 'Publish results', 'success',
           confirm='Publish the certified results publicly?', effect=_effect_publish),
    Action('archive', (S.PUBLISHED, S.CLOSED, S.DRAFT), S.ARCHIVED, 'election.archive', 'Archive',
           confirm='Archive this election? It becomes read-only.', guard=_guard_archive, effect=_effect_archive),
    Action('decertify', (S.CERTIFIED, S.PUBLISHED), S.TALLYING, 'results.certify', 'Decertify', manual=False),
    Action('reopen_voting', (S.CLOSED,), S.OPEN, 'election.close', 'Re-open voting', manual=False,
           effect=_effect_open),
]}


def available_actions(event, user):
    from core.rbac import has_perm

    return [a for a in ACTIONS.values()
            if a.manual and event.status in a.sources and has_perm(user, a.perm, event)]


def transition(event, action_name, *, actor=None, request=None, reason='', system=False, extra_effect=None,
               skip_guard=False):
    action = ACTIONS.get(action_name)
    if action is None:
        raise LifecycleError(f'Unknown action {action_name}.')
    with transaction.atomic():
        locked = Event.objects.select_for_update().get(pk=event.pk)
        if locked.status not in action.sources:
            raise LifecycleError(
                f'Cannot {action.label.lower()} while the election is {locked.get_status_display().lower()}.')
        if not system:
            check_perm(actor, action.perm, locked)
        if action.guard and not skip_guard:
            action.guard(locked, actor)
        previous = locked.status
        locked.status = action.target
        if action.effect:
            action.effect(locked, actor)
        if extra_effect:
            extra_effect(locked)
        locked.save()
        audit.record(f'ELECTION_{action_name.upper()}', request=request, actor=actor, event=locked,
                     summary=f'{action.label}: {previous} -> {locked.status}',
                     changes={'status': {'old': previous, 'new': locked.status}}, reason=reason,
                     metadata={'system': system})
        transaction.on_commit(lambda: _after_transition(locked.pk, action_name))
    event.refresh_from_db()
    return event


def _after_transition(event_id, action_name):
    from . import tasks

    if action_name in ('open', 'reopen_voting'):
        tasks.notify_voting_opened.delay(event_id)
    elif action_name == 'close':
        tasks.auto_tally.delay(event_id)
    elif action_name == 'publish':
        tasks.notify_results_published.delay(event_id)


def tick(event, now=None):
    """Apply time-based transitions (auto open / auto close). Safe to call on
    every request and from the scheduler."""
    now = now or timezone.now()
    try:
        if event.status == S.SCHEDULED and now >= event.start_date:
            # Catch-up (scheduler was down for the whole window): open without
            # the "end date passed" guard so the election still closes cleanly.
            event = transition(event, 'open', system=True, reason='Automatic opening at start time',
                               skip_guard=now >= event.end_date)
        if event.status in (S.OPEN, S.PAUSED) and now >= event.end_date:
            event = transition(event, 'close', system=True, reason='Automatic closing at end time')
    except LifecycleError:
        pass
    return event


EDITABLE_STATES = (S.DRAFT,)
RESETTING_STATES = (S.APPROVED, S.SCHEDULED)
LIVE_STATES = (S.OPEN, S.PAUSED)
LOCKED_STATES = (S.CLOSED, S.TALLYING, S.CERTIFIED, S.PUBLISHED, S.ARCHIVED)


def edit_policy(event, scope):
    """May ``scope`` ('config', 'ballot', 'candidates', 'voters', 'candidate_profile')
    be changed now? Returns (allowed, resets_approval, reason)."""
    frozen_flag = {'config': event.config_frozen, 'ballot': event.ballot_frozen,
                   'candidates': event.candidates_frozen, 'voters': event.voter_list_frozen,
                   'candidate_profile': event.candidates_frozen}.get(scope, False)
    if event.status in LOCKED_STATES:
        return False, False, f'The election is {event.get_status_display().lower()}; it can no longer be changed.'
    if event.status == S.REVIEW:
        return False, False, 'The election is in review. Withdraw it (or have it returned) before editing.'
    if frozen_flag:
        return False, False, f'The {scope.replace("_", " ")} is frozen. Request an unfreeze (dual approval) to change it.'
    if event.status in EDITABLE_STATES:
        return True, False, ''
    if event.status in RESETTING_STATES:
        if scope in ('voters', 'candidate_profile'):
            return True, False, ''
        return True, True, 'Changing an approved election returns it to draft for re-approval.'
    if event.status in LIVE_STATES:
        if scope in ('voters', 'candidate_profile'):
            return True, False, ''
        if event.is_paid and scope in ('candidates', 'ballot'):
            return True, False, ''
        return False, False, 'Voting is in progress: the configuration cannot change.'
    return False, False, 'Not editable in the current state.'


def require_editable(event, scope, actor=None, request=None):
    """Raise LifecycleError if not editable; reset approval when needed."""
    allowed, resets, reason = edit_policy(event, scope)
    if not allowed:
        raise LifecycleError(reason)
    if resets:
        previous = event.status
        Event.objects.filter(pk=event.pk).update(status=S.DRAFT, reviewed_by=None)
        event.status = S.DRAFT
        audit.record('ELECTION_APPROVAL_RESET', request=request, actor=actor, event=event,
                     summary=f'Configuration change returned election from {previous} to DRAFT',
                     changes={'status': {'old': previous, 'new': S.DRAFT}})
    return True
