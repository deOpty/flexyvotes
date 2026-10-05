"""Election ballot-key management.

SYSTEM custody: the X25519 private key is envelope-encrypted (DEK wrapped by
the KEK / AWS KMS) and only unsealed by the tally.

TRUSTEE custody: the private key is split with Shamir's scheme between the
election's trustees and never stored whole. Each trustee collects their
share once; at tally time at least ``threshold`` of them must submit it.
"""
from django.db import transaction
from django.utils import timezone

from core import audit, crypto
from voting.models import Event

from .models import ElectionKey, TrusteeShare


class KeyCustodyError(Exception):
    pass


def ensure_election_key(event, actor=None):
    if ElectionKey.objects.filter(election=event).exists():
        return event.ballot_key
    private_raw, public_b64 = crypto.generate_election_keypair()
    key = ElectionKey(election=event, public_key=public_b64, fingerprint=crypto.key_fingerprint(public_b64),
                      custody=event.key_custody)
    with transaction.atomic():
        if event.key_custody == Event.KeyCustody.TRUSTEES:
            trustees = list(TrusteeShare.objects.filter(election=event).order_by('index'))
            if len(trustees) < 2 or not 1 <= event.trustee_threshold <= len(trustees):
                raise KeyCustodyError('Configure at least two trustees and a valid threshold first.')
            shares = crypto.split_secret(private_raw, len(trustees), event.trustee_threshold)
            for trustee, share in zip(trustees, shares):
                trustee.share_hash = crypto.sha256_hex(share)
                trustee.pending_share = share
                trustee.save(update_fields=['share_hash', 'pending_share'])
            key.threshold, key.shares = event.trustee_threshold, len(trustees)
        else:
            key.wrapped_private_key = crypto.b64e(private_raw)
        key.save()
        audit.record('BALLOT_KEY_GENERATED', actor=actor, event=event, target=key,
                     summary=f'Ballot encryption key generated ({key.custody})',
                     metadata={'fingerprint': key.fingerprint, 'threshold': key.threshold, 'shares': key.shares})
    del private_raw
    event.ballot_key = key
    return key


def add_trustee(event, user, actor):
    if hasattr(event, 'ballot_key'):
        raise KeyCustodyError('Trustees cannot change after the ballot key has been generated.')
    index = (TrusteeShare.objects.filter(election=event).order_by('-index').values_list('index', flat=True).first() or 0) + 1
    share, created = TrusteeShare.objects.get_or_create(election=event, trustee=user, defaults={'index': index, 'share_hash': ''})
    if created:
        audit.record('TRUSTEE_ADDED', actor=actor, event=event, target=user, summary=f'Trustee {user.username} added')
    return share


def collect_share(event, user):
    """Hand the trustee their share exactly once."""
    with transaction.atomic():
        share = TrusteeShare.objects.select_for_update().filter(election=event, trustee=user).first()
        if share is None:
            raise KeyCustodyError('You are not a trustee for this election.')
        if share.collected_at or not share.pending_share:
            raise KeyCustodyError('Your key share has already been collected.')
        value = share.pending_share
        share.pending_share = None
        share.collected_at = timezone.now()
        share.save(update_fields=['pending_share', 'collected_at'])
    audit.record('TRUSTEE_SHARE_COLLECTED', actor=user, event=event, target=share, summary='Trustee collected key share')
    return value


def submit_share(event, user, value):
    share = TrusteeShare.objects.filter(election=event, trustee=user).first()
    if share is None:
        raise KeyCustodyError('You are not a trustee for this election.')
    if not crypto.constant_time_equals(crypto.sha256_hex(value.strip()), share.share_hash):
        audit.record('TRUSTEE_SHARE_REJECTED', actor=user, event=event, target=share, result='FAILURE',
                     summary='Submitted key share did not match')
        raise KeyCustodyError('That key share is not valid for this election.')
    share.submitted_share = value.strip()
    share.submitted_at = timezone.now()
    share.save(update_fields=['submitted_share', 'submitted_at'])
    audit.record('TRUSTEE_SHARE_SUBMITTED', actor=user, event=event, target=share, summary='Trustee submitted key share for tally')
    return share


def shares_status(event):
    key = getattr(event, 'ballot_key', None)
    submitted = TrusteeShare.objects.filter(election=event, submitted_at__isnull=False).count()
    return {'threshold': key.threshold if key else event.trustee_threshold, 'submitted': submitted,
            'trustees': TrusteeShare.objects.filter(election=event).count()}


def private_key_for_tally(event):
    try:
        key = event.ballot_key
    except ElectionKey.DoesNotExist:
        raise KeyCustodyError('This election has no ballot key.') from None
    if key.custody == Event.KeyCustody.TRUSTEES:
        shares = [s.submitted_share for s in TrusteeShare.objects.filter(election=event, submitted_at__isnull=False)]
        if len(shares) < key.threshold:
            raise KeyCustodyError(f'{key.threshold} trustee key shares are required; {len(shares)} submitted.')
        private_raw = crypto.combine_shares(shares[:key.threshold])
    else:
        private_raw = crypto.b64d(key.wrapped_private_key)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    derived = X25519PrivateKey.from_private_bytes(private_raw).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if crypto.b64e(derived) != key.public_key:
        raise KeyCustodyError('Reconstructed key does not match the election public key.')
    return private_raw


def wipe_submitted_shares(event):
    TrusteeShare.objects.filter(election=event).update(submitted_share=None)
