"""Delivery providers. Each returns (status, provider_message_id, error)."""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives

from core.http import safe_request

logger = logging.getLogger(__name__)

SENT, FAILED, SKIPPED = 'SENT', 'FAILED', 'SKIPPED'


def send_email(recipient, subject, text, html=None):
    if not recipient:
        return SKIPPED, '', 'No email address'
    message = EmailMultiAlternatives(subject, text, settings.DEFAULT_FROM_EMAIL, [recipient])
    if html:
        message.attach_alternative(html, 'text/html')
    try:
        message.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - SMTP errors are recorded and retried
        logger.warning('Email delivery failed: %s', exc.__class__.__name__)
        return FAILED, '', exc.__class__.__name__
    return SENT, '', ''


def send_sms(recipient, text):
    if not settings.AT_API_KEY:
        return SKIPPED, '', "Africa's Talking is not configured"
    host = 'api.sandbox.africastalking.com' if settings.AT_USERNAME == 'sandbox' else 'api.africastalking.com'
    data = {'username': settings.AT_USERNAME, 'to': recipient, 'message': text[:900]}
    if settings.AT_SENDER_ID:
        data['from'] = settings.AT_SENDER_ID
    try:
        response = safe_request('POST', f'https://{host}/version1/messaging', integration='sms', data=data,
                                headers={'apiKey': settings.AT_API_KEY, 'Accept': 'application/json'}, timeout=10)
        body = response.json()
        recipients = body.get('SMSMessageData', {}).get('Recipients', [])
        if response.status_code < 300 and recipients and recipients[0].get('status') in ('Success', 'Sent'):
            return SENT, recipients[0].get('messageId', ''), ''
        return FAILED, '', str(body.get('SMSMessageData', {}).get('Message', response.status_code))[:300]
    except Exception as exc:  # noqa: BLE001
        return FAILED, '', exc.__class__.__name__


def send_whatsapp(recipient, text):
    if not (settings.WHATSAPP_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID):
        return SKIPPED, '', 'WhatsApp is not configured'
    try:
        response = safe_request(
            'POST', f'https://graph.facebook.com/v20.0/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages',
            integration='whatsapp', timeout=10,
            headers={'Authorization': f'Bearer {settings.WHATSAPP_TOKEN}'},
            json={'messaging_product': 'whatsapp', 'to': recipient.lstrip('+'), 'type': 'text',
                  'text': {'body': text[:4000]}},
        )
        body = response.json()
        if response.status_code < 300 and body.get('messages'):
            return SENT, body['messages'][0].get('id', ''), ''
        return FAILED, '', str(body.get('error', {}).get('message', response.status_code))[:300]
    except Exception as exc:  # noqa: BLE001
        return FAILED, '', exc.__class__.__name__
