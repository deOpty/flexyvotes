import logging
from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)


def _fmt(dt):
    return timezone.localtime(dt).strftime('%d %b %Y %H:%M %Z')


@shared_task(name='elections.tasks.lifecycle_tick')
def lifecycle_tick():
    """Automatic opening and closing of elections."""
    from voting.models import Event

    from . import lifecycle

    now = timezone.now()
    due = Event.objects.filter(
        Q(status=Event.Status.SCHEDULED, start_date__lte=now)
        | Q(status__in=[Event.Status.OPEN, Event.Status.PAUSED], end_date__lte=now))
    changed = 0
    for event in due:
        before = event.status
        if lifecycle.tick(event, now).status != before:
            changed += 1
    return changed


@shared_task(name='elections.tasks.auto_tally')
def auto_tally(event_id):
    """Automatic tallying when voting closes (system-custody keys only;
    trustee elections wait for the trustees to submit their shares)."""
    from voting.models import Event

    from .keys import KeyCustodyError
    from .results import ResultsError, run_tally

    event = Event.objects.filter(pk=event_id).first()
    if event is None or event.status != Event.Status.CLOSED:
        return None
    if event.is_institutional and event.key_custody == Event.KeyCustody.TRUSTEES:
        logger.info('Election %s awaits trustee key shares before tallying', event_id)
        return 'awaiting-trustees'
    try:
        result = run_tally(event, system=True)
    except (KeyCustodyError, ResultsError) as exc:
        logger.error('Automatic tally failed for election %s: %s', event_id, exc)
        return None
    return str(result.pk)


def _voter_link(event):
    from .voters import vote_link

    return vote_link(event)


def _notify_voters(event, template, voters, dedupe_prefix):
    from notifications.models import Notification
    from notifications.service import notify

    from .models import Voter

    sent = 0
    context = {'ends': _fmt(event.end_date), 'link': _voter_link(event)}
    for voter in voters.iterator(chunk_size=500):
        if voter.email:
            channel, recipient = Notification.Channel.EMAIL, voter.email
        elif voter.phone:
            channel, recipient = Notification.Channel.SMS, voter.phone
        else:
            continue
        if notify(template, channel=channel, recipient=recipient, event=event, context=context,
                  dedupe_key=f'{dedupe_prefix}:{voter.pk}'):
            sent += 1
        Voter.objects.filter(pk=voter.pk).update(last_reminded_at=timezone.now())
    return sent


@shared_task(name='elections.tasks.notify_voting_opened')
def notify_voting_opened(event_id):
    from voting.models import Event

    from .models import Voter

    event = Event.objects.filter(pk=event_id).first()
    if event is None or not event.is_institutional:
        return 0
    voters = event.voters.filter(status__in=[Voter.Status.ELIGIBLE, Voter.Status.VERIFIED])
    return _notify_voters(event, 'voting_opened', voters, f'opened:{event.pk}:{event.opened_at:%Y%m%d%H%M}')


@shared_task(name='elections.tasks.send_voting_reminders')
def send_voting_reminders():
    """24h-before reminder and 2h-before "closing soon" notice to non-voters."""
    from voting.models import Event

    from .models import Voter

    now = timezone.now()
    total = 0
    for event in Event.objects.filter(status=Event.Status.OPEN, voting_mode=Event.VotingMode.CODE_VOTING,
                                      end_date__gt=now, end_date__lte=now + timedelta(hours=24)):
        remaining = event.end_date - now
        template = 'election_closing' if remaining <= timedelta(hours=2) else 'voting_reminder'
        voters = event.voters.filter(status__in=[Voter.Status.ELIGIBLE, Voter.Status.VERIFIED]).filter(
            Q(last_reminded_at__isnull=True) | Q(last_reminded_at__lt=now - timedelta(hours=1 if template == 'election_closing' else 12)))
        total += _notify_voters(event, template, voters, f'{template}:{event.pk}')
    return total


@shared_task(name='elections.tasks.send_vote_confirmation')
def send_vote_confirmation(event_id, voter_id):
    from django.conf import settings

    from notifications.models import Notification
    from notifications.service import notify
    from voting.models import Event

    from .models import Voter

    event = Event.objects.filter(pk=event_id).first()
    voter = Voter.objects.filter(pk=voter_id).first()
    if not (event and voter):
        return None
    # Deliberately no tracker / choices: confirmation of participation only.
    context = {'cast_at': _fmt(voter.voted_at or timezone.now()),
               'verify_link': f'{settings.SITE_URL}/verify/{event.pk}/'}
    if voter.email:
        return bool(notify('vote_confirmation', channel=Notification.Channel.EMAIL, recipient=voter.email,
                           event=event, context=context, dedupe_key=f'confirm:{event.pk}:{voter.pk}'))
    if voter.phone:
        return bool(notify('vote_confirmation', channel=Notification.Channel.SMS, recipient=voter.phone,
                           event=event, context=context, dedupe_key=f'confirm:{event.pk}:{voter.pk}'))
    return False


@shared_task(name='elections.tasks.notify_results_published')
def notify_results_published(event_id):
    from django.conf import settings

    from notifications.service import notify_user
    from voting.models import Event

    event = Event.objects.filter(pk=event_id).first()
    if event is None:
        return 0
    link = f'{settings.SITE_URL}/results/{event.pk}/'
    sent = 0
    if event.is_institutional:
        context = {'ends': _fmt(event.end_date), 'link': link}
        from notifications.models import Notification
        from notifications.service import notify

        for voter in event.voters.exclude(email='').exclude(email__isnull=True).iterator(chunk_size=500):
            if voter.email and notify('results_published', channel=Notification.Channel.EMAIL, recipient=voter.email,
                                      event=event, context=context, dedupe_key=f'results:{event.pk}:{voter.pk}'):
                sent += 1
    for candidate in event.candidates.exclude(user__isnull=True).select_related('user'):
        notify_user(candidate.user, 'results_published', {'link': link}, event=event,
                    dedupe_key=f'results:{event.pk}:cand:{candidate.pk}')
        sent += 1
    return sent
