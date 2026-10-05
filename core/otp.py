"""Email / SMS one-time passcodes."""
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from . import crypto, ratelimit
from .models import OTPChallenge
from .utils import mask_email, mask_phone

OTP_TTL = timedelta(minutes=10)
RESEND_COOLDOWN_SECONDS = 60
MAX_PER_HOUR = 6


class OTPError(Exception):
    pass


def _hash(challenge_id, code):
    return crypto.keyed_hash('otp', f'{challenge_id}:{code}')


def issue(purpose, subject_type, subject_id, channel, destination, *, label='', event=None, organization=None):
    """Create a challenge and queue the code for delivery. Returns the challenge."""
    if not destination:
        raise OTPError('No contact address on file for this channel.')
    ident = f'{subject_type}:{subject_id}:{purpose}'
    allowed, _ = ratelimit.hit('otp-issue', ident, MAX_PER_HOUR, 3600)
    if not allowed:
        raise OTPError('Too many codes requested. Please wait before requesting another.')
    recent = OTPChallenge.objects.filter(subject_type=subject_type, subject_id=str(subject_id), purpose=purpose,
                                         created_at__gte=timezone.now() - timedelta(seconds=RESEND_COOLDOWN_SECONDS)).exists()
    if recent:
        raise OTPError('A code was just sent. Please wait a minute before requesting another.')
    code = crypto.numeric_code(6)
    challenge_id = uuid.uuid4()
    hint = mask_email(destination) if channel == OTPChallenge.Channel.EMAIL else mask_phone(destination)
    challenge = OTPChallenge.objects.create(
        id=challenge_id, purpose=purpose, subject_type=subject_type, subject_id=str(subject_id), channel=channel,
        destination_hint=hint, code_hash=_hash(challenge_id, code), expires_at=timezone.now() + OTP_TTL,
    )
    from notifications.service import notify

    notify('verification_otp', channel=channel, recipient=destination, event=event, organization=organization,
           context={'code': code, 'label': label or 'verification', 'minutes': int(OTP_TTL.total_seconds() // 60)},
           immediate=True)
    return challenge


def verify(challenge_id, code, *, purpose, subject_type, subject_id):
    """Return True when the code is right. Each wrong guess burns an attempt."""
    code = (code or '').strip().replace(' ', '')
    with transaction.atomic():
        challenge = OTPChallenge.objects.select_for_update().filter(
            pk=challenge_id, purpose=purpose, subject_type=subject_type, subject_id=str(subject_id)).first()
        if challenge is None or challenge.consumed_at is not None:
            raise OTPError('This code is no longer valid. Request a new one.')
        if challenge.expires_at <= timezone.now():
            raise OTPError('This code has expired. Request a new one.')
        if challenge.attempts >= challenge.max_attempts:
            raise OTPError('Too many incorrect attempts. Request a new code.')
        challenge.attempts += 1
        ok = crypto.constant_time_equals(_hash(challenge.pk, code), challenge.code_hash)
        if ok:
            challenge.consumed_at = timezone.now()
        challenge.save(update_fields=['attempts', 'consumed_at'])
    if not ok:
        remaining = challenge.max_attempts - challenge.attempts
        raise OTPError(f'Incorrect code. {remaining} attempt(s) left.' if remaining else
                       'Too many incorrect attempts. Request a new code.')
    return challenge
