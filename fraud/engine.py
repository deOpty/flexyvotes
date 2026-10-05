"""Risk engine.

Each assessment adds up weighted signals into a 0-100 score:

    0-30   ALLOW      normal
    31-60  MONITOR    recorded for analysts
    61-80  CHALLENGE  extra verification (CAPTCHA / OTP) before continuing
    81-100 HOLD       money accepted, votes NOT credited until a human reviews

Suspicious activity is never silently deleted - it is held and reviewed.
"""
import ipaddress
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.db.models import Q, Sum
from django.utils import timezone

from core import crypto, metrics, ratelimit
from core.utils import client_ip, device_hash as request_device_hash, user_agent

from .models import BlocklistEntry, FraudEvent

logger = logging.getLogger(__name__)

D = FraudEvent.Decision

BOT_UA_MARKERS = ('curl/', 'python-requests', 'python-urllib', 'wget', 'go-http-client', 'httpclient', 'okhttp',
                  'java/', 'headlesschrome', 'phantomjs', 'selenium', 'puppeteer', 'playwright', 'scrapy',
                  'spider', 'crawler', 'libwww', 'httpie', 'postman')


@lru_cache(maxsize=1)
def disposable_domains():
    path = Path(__file__).parent / 'data' / 'disposable_domains.txt'
    return frozenset(line.strip().lower() for line in path.read_text().splitlines()
                     if line.strip() and not line.startswith('#'))


@dataclass
class Assessment:
    score: int = 0
    decision: str = D.ALLOW
    signals: list = field(default_factory=list)
    event_id: int = None

    def add(self, code, weight, detail=''):
        self.signals.append({'code': code, 'weight': weight, 'detail': detail})

    @property
    def held(self):
        return self.decision == D.HOLD

    @property
    def challenged(self):
        return self.decision == D.CHALLENGE


def decide(score):
    if score >= settings.FRAUD_HOLD_THRESHOLD:
        return D.HOLD
    if score >= settings.FRAUD_CHALLENGE_THRESHOLD:
        return D.CHALLENGE
    if score >= settings.FRAUD_MONITOR_THRESHOLD:
        return D.MONITOR
    return D.ALLOW


def _count(scope, ident, window):
    """Increment and return a velocity counter."""
    if not ident:
        return 0
    ratelimit.hit(f'fraud:{scope}', ident, 10 ** 9, window)
    return ratelimit.peek(f'fraud:{scope}', ident, window)


def _active_entries(org_id):
    """Platform-wide entries plus those of the event's own organization."""
    scope = Q(organization__isnull=True)
    if org_id:
        scope |= Q(organization_id=org_id)
    return BlocklistEntry.objects.filter(scope, is_active=True).exclude(expires_at__lt=timezone.now())


def _blocklisted(kind, value, org_id=None):
    if not value:
        return None
    return _active_entries(org_id).filter(kind=kind, value=value).first()


def _ip_in_ranges(ip, kinds, org_id=None):
    if not ip:
        return None
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    for entry in _active_entries(org_id).filter(kind__in=kinds):
        try:
            if address in ipaddress.ip_network(entry.value, strict=False):
                return entry
        except ValueError:
            continue
    return None


def assess(kind, *, request=None, event=None, candidate=None, email='', phone='', device='', ip=None,
           card_signature='', card_country='', amount=None, votes=0, payment=None, count_velocity=True,
           record=True):
    from payments.models import Payment

    result = Assessment()
    ip = ip or client_ip(request)
    device = device or (request_device_hash(request) if request is not None else '')
    ua = user_agent(request).lower() if request is not None else None
    email = (email or '').strip().lower()
    email_index = crypto.blind_index(email, 'email') if email else ''
    phone_index = crypto.blind_index(phone, 'phone') if phone else ''

    # -- blocklists -------------------------------------------------------
    org_id = getattr(event, 'organization_id', None)
    for entry in (_blocklisted('IP', ip, org_id), _ip_in_ranges(ip, ['CIDR'], org_id),
                  _blocklisted('EMAIL', email_index, org_id), _blocklisted('PHONE', phone_index, org_id),
                  _blocklisted('DEVICE', device, org_id), _blocklisted('CARD', card_signature, org_id)):
        if entry:
            result.add('blocklisted', 60, f'{entry.get_kind_display()}: {entry.reason or "listed"}')
    domain = email.rsplit('@', 1)[-1] if '@' in email else ''
    if domain and _blocklisted('EMAIL_DOMAIN', domain, org_id):
        result.add('blocklisted_domain', 60, domain)
    if settings.FRAUD_FLAG_PROXIES:
        if _ip_in_ranges(ip, ['ANONYMIZER'], org_id):
            result.add('anonymizer', 25, 'Tor exit / VPN / proxy range')
        if request is not None and (request.META.get('HTTP_VIA') or request.META.get('HTTP_FORWARDED')):
            result.add('proxy_headers', 10, 'Request relayed through an open proxy')

    # -- identity quality -------------------------------------------------
    if domain and domain in disposable_domains():
        result.add('disposable_email', 35, domain)
    if ua is not None:
        if not ua:
            result.add('missing_user_agent', 15)
        elif any(marker in ua for marker in BOT_UA_MARKERS):
            result.add('automation_user_agent', 30, ua[:60])

    # -- velocity -----------------------------------------------------------
    if count_velocity:
        ip_count = _count(f'{kind}:ip', ip, 600)
        device_count = _count(f'{kind}:device', device, 600)
        email_count = _count(f'{kind}:email', email_index, 600)
        if ip_count > 30:
            result.add('ip_velocity_high', 35, f'{ip_count} attempts from this IP in 10 min')
        elif ip_count > 10:
            result.add('ip_velocity', 20, f'{ip_count} attempts from this IP in 10 min')
        if device_count > 10:
            result.add('device_velocity', 20, f'{device_count} attempts from this device in 10 min')
        if email_count > 15:
            result.add('email_velocity', 15, f'{email_count} attempts by this email in 10 min')

    # -- payment history (database) -----------------------------------------
    now = timezone.now()
    if kind in (FraudEvent.Kind.PAYMENT,) and (email_index or device):
        recent = Payment.objects.filter(created_at__gte=now - timedelta(hours=1),
                                        status__in=[Payment.Status.FAILED, Payment.Status.ABANDONED])
        failed = recent.filter(payer_email_index=email_index).count() if email_index else 0
        failed = max(failed, recent.filter(device_hash=device).count() if device else 0)
        if failed >= 6:
            result.add('repeated_failed_payments', 25, f'{failed} failed/abandoned in the last hour')
        elif failed >= 3:
            result.add('failed_payments', 15, f'{failed} failed/abandoned in the last hour')
        day = Payment.objects.filter(created_at__gte=now - timedelta(hours=24))
        if device:
            emails_on_device = day.filter(device_hash=device).exclude(payer_email_index='') \
                .values('payer_email_index').distinct().count()
            if emails_on_device >= 4:
                result.add('device_shared_by_accounts', 25, f'{emails_on_device} payer emails on one device (account farm)')
        if ip:
            emails_on_ip = day.filter(ip_address=ip, created_at__gte=now - timedelta(hours=1)).exclude(
                payer_email_index='').values('payer_email_index').distinct().count()
            if emails_on_ip >= 8:
                result.add('ip_shared_by_accounts', 15, f'{emails_on_ip} payer emails from one IP in an hour')
        history = Payment.objects.filter(status__in=[Payment.Status.DISPUTED, Payment.Status.REVERSED])
        if (email_index and history.filter(payer_email_index=email_index).exists()) or \
                (card_signature and history.filter(card_signature=card_signature).exists()):
            result.add('chargeback_history', 50, 'Previous chargeback / reversal')
    if card_signature:
        emails_on_card = Payment.objects.filter(card_signature=card_signature, created_at__gte=now - timedelta(hours=24)) \
            .exclude(payer_email_index='').values('payer_email_index').distinct().count()
        if emails_on_card >= 3:
            result.add('card_shared_by_accounts', 30, f'One card used by {emails_on_card} payer emails in 24h')
        if event is not None and event.max_votes_per_voter:
            card_votes = Payment.objects.filter(event=event, card_signature=card_signature, votes_credited=True) \
                .aggregate(total=Sum('votes'))['total'] or 0
            if card_votes + votes > event.max_votes_per_voter:
                result.add('card_vote_limit', 85, f'Card exceeds the {event.max_votes_per_voter}-vote limit')
    if card_country and event is not None and event.allowed_countries and \
            card_country.upper() not in [c.upper() for c in event.allowed_countries]:
        result.add('unexpected_country', 20, f'Card/IP country {card_country}')
    if amount is not None and amount >= 1000:
        result.add('high_amount', 10, f'Amount {amount}')

    # -- traffic pattern for the target candidate ----------------------------
    if candidate is not None and kind == FraudEvent.Kind.PAYMENT:
        burst = _count('candidate-votes', str(candidate.pk), 300)
        if burst > 200:
            result.add('candidate_vote_burst', 15, f'{burst} payment attempts for this candidate in 5 min')

    result.score = min(100, sum(s['weight'] for s in result.signals))
    result.decision = decide(result.score)
    if result.decision != D.ALLOW:
        metrics.FRAUD_ALERTS.labels(decision=result.decision).inc()
    if record and result.decision != D.ALLOW:
        fraud_event = FraudEvent.objects.create(
            kind=kind, decision=result.decision, score=result.score, signals=result.signals, event=event,
            payment=payment, candidate=candidate, ip_address=ip, device_hash=device or '',
            subject=_mask(email) or phone[-4:] if (email or phone) else '',
        )
        result.event_id = fraud_event.pk
    return result


def _mask(email):
    from core.utils import mask_email

    return mask_email(email)


def record_chargeback(payment, reason):
    event = FraudEvent.objects.create(
        kind=FraudEvent.Kind.CHARGEBACK, decision=D.HOLD, score=100, payment=payment, event=payment.event,
        candidate=payment.candidate, signals=[{'code': 'chargeback', 'weight': 100, 'detail': reason}],
        subject=_mask(payment.payer_email),
    )
    metrics.FRAUD_ALERTS.labels(decision='HOLD').inc()
    return event


def anomaly_scan(now=None):
    """Look for vote bursts / coordinated campaigns on open paid elections."""
    from django.core.cache import cache

    from payments.models import Payment
    from voting.models import Event, VoteTransaction

    now = now or timezone.now()
    found = 0
    for event in Event.objects.filter(status=Event.Status.OPEN, voting_mode=Event.VotingMode.PAY_TO_VOTE):
        recent_start, baseline_start = now - timedelta(minutes=5), now - timedelta(minutes=65)
        ledger = VoteTransaction.objects.filter(candidate__event=event, status='Success', vote_type='Main')
        for candidate in event.candidates.all():
            recent = ledger.filter(candidate=candidate, created_at__gte=recent_start).aggregate(t=Sum('number_of_votes'))['t'] or 0
            baseline = ledger.filter(candidate=candidate, created_at__gte=baseline_start, created_at__lt=recent_start) \
                .aggregate(t=Sum('number_of_votes'))['t'] or 0
            per_window = baseline / 12.0
            if recent >= 100 and recent > 5 * max(per_window, 1):
                key = f'fraud:anomaly:burst:{candidate.pk}:{now:%Y%m%d%H}'
                if cache.add(key, 1, 3600):
                    FraudEvent.objects.create(
                        kind=FraudEvent.Kind.ANOMALY, decision=D.MONITOR, score=50, event=event, candidate=candidate,
                        signals=[{'code': 'vote_burst', 'weight': 50,
                                  'detail': f'{recent} votes in 5 min vs ~{per_window:.0f} baseline'}])
                    found += 1
            devices = Payment.objects.filter(candidate=candidate, created_at__gte=recent_start) \
                .exclude(device_hash='').values('device_hash').distinct().count()
            if devices >= 30:
                key = f'fraud:anomaly:coord:{candidate.pk}:{now:%Y%m%d%H}'
                if cache.add(key, 1, 3600):
                    FraudEvent.objects.create(
                        kind=FraudEvent.Kind.ANOMALY, decision=D.MONITOR, score=55, event=event, candidate=candidate,
                        signals=[{'code': 'coordinated_campaign', 'weight': 55,
                                  'detail': f'{devices} distinct devices paying for one candidate in 5 min'}])
                    found += 1
        if event.allowed_countries:
            hour = Payment.objects.filter(event=event, status=Payment.Status.SUCCESS, created_at__gte=now - timedelta(hours=1)) \
                .exclude(card_country='')
            total = hour.count()
            foreign = hour.exclude(card_country__in=event.allowed_countries).count()
            if total >= 20 and foreign / total > 0.5:
                key = f'fraud:anomaly:geo:{event.pk}:{now:%Y%m%d%H}'
                if cache.add(key, 1, 3600):
                    FraudEvent.objects.create(
                        kind=FraudEvent.Kind.ANOMALY, decision=D.MONITOR, score=45, event=event,
                        signals=[{'code': 'unusual_geography', 'weight': 45,
                                  'detail': f'{foreign} of {total} payments in the last hour from outside allowed countries'}])
                    found += 1
    return found
