import responses
from django.core import mail
from django.test import TestCase, override_settings

from core.tests.factories import make_event
from notifications.models import Notification
from notifications.service import deliver, notify


class NotificationTests(TestCase):
    def test_email_is_queued_then_delivered_and_secrets_wiped(self):
        event = make_event()
        with self.captureOnCommitCallbacks(execute=True):
            notification = notify('voter_invitation', channel='EMAIL', recipient='voter@uni.edu', event=event,
                                  context={'code': 'SECRET123', 'name': 'Ama', 'link': 'https://x', 'starts': 'a', 'ends': 'b'})
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.SENT)
        self.assertIsNone(notification.context)
        self.assertEqual(notification.recipient_hint, 'v****@uni.edu')
        self.assertIn('SECRET123', mail.outbox[0].body)
        self.assertEqual(len(mail.outbox[0].alternatives), 1)
        self.assertIn(event.title, mail.outbox[0].subject)

    def test_dedupe_key_prevents_duplicates(self):
        first = notify('voting_reminder', channel='EMAIL', recipient='a@b.com', dedupe_key='r:1')
        second = notify('voting_reminder', channel='EMAIL', recipient='a@b.com', dedupe_key='r:1')
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_sms_skipped_when_not_configured(self):
        notification = notify('vote_confirmation', channel='SMS', recipient='+233241234567', context={'election': 'X'})
        deliver(notification.pk)
        notification.refresh_from_db()
        self.assertEqual(notification.status, Notification.Status.SKIPPED)

    @override_settings(AT_API_KEY='at-key', AT_USERNAME='sandbox')
    @responses.activate
    def test_sms_via_africas_talking(self):
        responses.add(responses.POST, 'https://api.sandbox.africastalking.com/version1/messaging', json={
            'SMSMessageData': {'Message': 'Sent', 'Recipients': [{'status': 'Success', 'messageId': 'ATX1'}]}})
        notification = notify('voting_opened', channel='SMS', recipient='+233241234567',
                              context={'election': 'SRC', 'link': 'https://x'})
        deliver(notification.pk)
        notification.refresh_from_db()
        self.assertEqual((notification.status, notification.provider_message_id), ('SENT', 'ATX1'))
        self.assertEqual(responses.calls[0].request.headers['apiKey'], 'at-key')

    @override_settings(WHATSAPP_TOKEN='wa', WHATSAPP_PHONE_NUMBER_ID='123')
    @responses.activate
    def test_whatsapp_channel(self):
        responses.add(responses.POST, 'https://graph.facebook.com/v20.0/123/messages', json={'messages': [{'id': 'wamid.1'}]})
        notification = notify('results_published', channel='WHATSAPP', recipient='+233241234567',
                              context={'election': 'SRC', 'link': 'https://x'})
        deliver(notification.pk)
        notification.refresh_from_db()
        self.assertEqual(notification.status, 'SENT')

    def test_in_app_notifications(self):
        from core.tests.factories import make_user
        from notifications.service import notify_user

        user = make_user('inbox')
        notify_user(user, 'security_alert', {'title': 'New sign-in', 'detail': 'x'}, channels=('IN_APP',))
        self.assertEqual(user.notifications.filter(channel='IN_APP', status='SENT').count(), 1)
