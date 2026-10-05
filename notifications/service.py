"""Notification outbox API.

Callers create notifications inside their own transaction; delivery happens
after commit on the Celery ``notifications`` queue, so a slow SMTP server can
never slow down (or roll back) a vote or payment.
"""
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.template import TemplateDoesNotExist
from django.template.loader import render_to_string
from django.utils import timezone

from core import metrics
from core.utils import mask_email, mask_phone

from .models import Notification

logger = logging.getLogger(__name__)

SUBJECTS = {
    'voter_invitation': 'You are invited to vote: {election}',
    'voter_registration': 'Voter registration confirmed: {election}',
    'verification_otp': 'Your verification code',
    'voting_opened': 'Voting is now open: {election}',
    'voting_reminder': 'Reminder: you have not voted yet in {election}',
    'election_closing': 'Last chance to vote: {election} closes soon',
    'vote_confirmation': 'Your vote was recorded: {election}',
    'payment_confirmation': 'Payment received - your votes are in',
    'results_published': 'Results published: {election}',
    'security_alert': 'Security alert: {title}',
    'organizer_registered': 'New organizer registration pending approval',
    'organizer_approved': 'Your organizer account is approved',
    'candidate_invitation': 'Set up your candidate profile: {election}',
    'approval_requested': 'Approval needed: {action}',
    'support_ticket': 'Support request {reference}: {subject}',
}


def _hint(channel, recipient):
    if channel == Notification.Channel.EMAIL:
        return mask_email(recipient)
    if channel in (Notification.Channel.SMS, Notification.Channel.WHATSAPP):
        return mask_phone(recipient)
    return ''


def notify(template, *, channel, recipient='', context=None, event=None, organization=None, user=None,
           dedupe_key=None, immediate=False, scheduled_for=None):
    context = dict(context or {})
    if event is not None:
        context.setdefault('election', event.title)
        context.setdefault('election_id', event.pk)
        organization = organization or event.organization
    context.setdefault('platform', settings.PLATFORM_NAME)
    context.setdefault('site_url', settings.SITE_URL)
    subject = SUBJECTS.get(template, settings.PLATFORM_NAME)
    try:
        subject = subject.format(**{k: v for k, v in context.items() if isinstance(v, (str, int))})
    except (KeyError, IndexError):
        pass
    try:
        with transaction.atomic():
            notification = Notification.objects.create(
                organization=organization, event=event, user=user, channel=channel, template=template,
                recipient=recipient or '', recipient_hint=_hint(channel, recipient), context=context,
                subject=subject[:200], dedupe_key=dedupe_key, scheduled_for=scheduled_for,
                status=Notification.Status.SENT if channel == Notification.Channel.IN_APP else Notification.Status.QUEUED,
                sent_at=timezone.now() if channel == Notification.Channel.IN_APP else None,
            )
    except IntegrityError:
        return None  # already queued under this dedupe key
    if channel != Notification.Channel.IN_APP:
        from . import tasks

        transaction.on_commit(lambda: tasks.deliver.apply_async(args=[str(notification.pk)],
                                                                priority=9 if immediate else 5))
    return notification


def notify_user(user, template, context=None, channels=('IN_APP', 'EMAIL'), event=None, dedupe_key=None):
    results = []
    for channel in channels:
        if channel == 'EMAIL' and not user.email:
            continue
        results.append(notify(template, channel=channel, recipient=user.email if channel == 'EMAIL' else '',
                              context=context, event=event, user=user,
                              dedupe_key=f'{dedupe_key}:{channel}' if dedupe_key else None))
    return results


def notify_platform_admins(template, context=None):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    for admin in User.objects.filter(is_active=True, is_superuser=True):
        notify_user(admin, template, context)


def notify_approvers(approval_request):
    from core.models import RoleAssignment

    event = approval_request.election
    query = RoleAssignment.objects.filter(role__permissions__contains=['approval.decide']) \
        if _json_contains_supported() else RoleAssignment.objects.all()
    users = set()
    for assignment in query.select_related('role', 'user'):
        if 'approval.decide' not in assignment.role.permissions:
            continue
        in_scope = (assignment.organization_id is None and assignment.event_id is None) or \
            (event is not None and assignment.event_id == event.pk) or \
            (assignment.organization_id and assignment.organization_id == approval_request.organization_id
             and assignment.event_id is None)
        if in_scope and assignment.user_id != approval_request.requested_by_id:
            users.add(assignment.user)
    context = {'action': approval_request.get_action_display(), 'reason': approval_request.reason,
               'requested_by': approval_request.requested_by.username,
               'link': f'{settings.SITE_URL}/console/approvals/'}
    for user in users:
        notify_user(user, 'approval_requested', context, event=event)


def _json_contains_supported():
    from django.db import connection

    return connection.vendor == 'postgresql'


def render(notification):
    context = dict(notification.context or {})
    base = f'notifications/{notification.template}'
    text = render_to_string(f'{base}.txt', context).strip()
    html = None
    if notification.channel == Notification.Channel.EMAIL:
        try:
            html = render_to_string(f'{base}.html', context)
        except TemplateDoesNotExist:
            html = render_to_string('notifications/base_email.html', {**context, 'body': text,
                                                                       'subject': notification.subject})
    else:
        try:
            text = render_to_string(f'{base}.sms.txt', context).strip()
        except TemplateDoesNotExist:
            pass
    return notification.subject, text, html


def deliver(notification_id):
    from . import channels

    with transaction.atomic():
        notification = Notification.objects.select_for_update().filter(pk=notification_id).first()
        if notification is None or notification.status not in (Notification.Status.QUEUED, Notification.Status.FAILED):
            return None
        if notification.scheduled_for and notification.scheduled_for > timezone.now():
            return notification
        subject, text, html = render(notification)
        if notification.channel == Notification.Channel.EMAIL:
            status, message_id, error = channels.send_email(notification.recipient, subject, text, html)
        elif notification.channel == Notification.Channel.SMS:
            status, message_id, error = channels.send_sms(notification.recipient, text)
        elif notification.channel == Notification.Channel.WHATSAPP:
            status, message_id, error = channels.send_whatsapp(notification.recipient, text)
        else:
            status, message_id, error = channels.SENT, '', ''
        notification.attempts += 1
        notification.status = status
        notification.provider_message_id = message_id
        notification.last_error = error
        if status in (channels.SENT, channels.SKIPPED):
            notification.sent_at = timezone.now() if status == channels.SENT else None
            # One-time codes and credentials must not outlive delivery.
            notification.context = None
        notification.save()
    metrics.NOTIFICATIONS.labels(channel=notification.channel, status=status).inc()
    if status == channels.SENT and notification.organization_id:
        from billing.service import record_usage

        metric = 'EMAILS_SENT' if notification.channel == Notification.Channel.EMAIL else 'SMS_SENT'
        if notification.channel in (Notification.Channel.EMAIL, Notification.Channel.SMS):
            record_usage(notification.organization, metric, 1, event=notification.event)
    return notification
