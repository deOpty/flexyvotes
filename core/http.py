"""Outbound HTTP with SSRF protection, timeouts and circuit breaking.

Every call to a third party (Paystack, identity providers, SMS, directory
services) goes through :func:`safe_request`:

* only https, and only to allow-listed hosts;
* hosts configured by tenants (custom OIDC issuers, directory APIs) must
  additionally resolve to public IP addresses - never the metadata service,
  localhost or the VPC;
* redirects are not followed;
* a per-integration circuit breaker fails fast while a provider is down so
  request threads don't pile up waiting on timeouts.
"""
import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class OutboundRequestBlocked(Exception):
    pass


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    def __init__(self, name, failure_threshold=5, window=60, reset_timeout=30):
        self.name = name
        self.failure_threshold = failure_threshold
        self.window = window
        self.reset_timeout = reset_timeout

    @property
    def _open_key(self):
        return f'cb:{self.name}:open_until'

    @property
    def _fail_key(self):
        return f'cb:{self.name}:failures'

    def is_open(self):
        return (cache.get(self._open_key) or 0) > time.time()

    def record_success(self):
        cache.delete(self._fail_key)

    def record_failure(self):
        if cache.add(self._fail_key, 1, self.window):
            failures = 1
        else:
            try:
                failures = cache.incr(self._fail_key)
            except ValueError:
                failures = 1
        if failures >= self.failure_threshold:
            cache.set(self._open_key, time.time() + self.reset_timeout, self.reset_timeout + 5)
            cache.delete(self._fail_key)
            logger.error('Circuit breaker %s opened after %s failures', self.name, failures)

    def state(self):
        return 'open' if self.is_open() else 'closed'


BREAKERS = {}


def breaker(name):
    if name not in BREAKERS:
        BREAKERS[name] = CircuitBreaker(name)
    return BREAKERS[name]


def _is_public_address(host):
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise OutboundRequestBlocked(f'Cannot resolve {host}') from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def validate_url(url, extra_hosts=None):
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise OutboundRequestBlocked('Only https outbound requests are allowed.')
    host = (parsed.hostname or '').lower()
    if not host:
        raise OutboundRequestBlocked('URL has no host.')
    static_allowed = {h.lower() for h in settings.OUTBOUND_HTTP_ALLOWED_HOSTS}
    dynamic_allowed = {h.lower() for h in (extra_hosts or [])}
    if host in static_allowed:
        return host
    if host in dynamic_allowed:
        try:
            ipaddress.ip_address(host)
            raise OutboundRequestBlocked('Raw IP hosts are not allowed.')
        except ValueError:
            pass
        if not _is_public_address(host):
            raise OutboundRequestBlocked(f'{host} resolves to a non-public address.')
        return host
    raise OutboundRequestBlocked(f'Host {host} is not on the outbound allow-list.')


def safe_request(method, url, *, integration='default', extra_hosts=None, timeout=15, **kwargs):
    validate_url(url, extra_hosts)
    circuit = breaker(integration)
    if circuit.is_open():
        raise CircuitOpen(f'{integration} is temporarily unavailable.')
    kwargs.setdefault('allow_redirects', False)
    try:
        response = requests.request(method, url, timeout=timeout, **kwargs)
    except requests.RequestException:
        circuit.record_failure()
        raise
    if response.status_code >= 500:
        circuit.record_failure()
    else:
        circuit.record_success()
    return response
