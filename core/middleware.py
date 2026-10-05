import logging
import re
import secrets
import time
import zoneinfo

from django.conf import settings
from django.contrib.auth import logout
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

from . import metrics
from .observability import correlation_id_var, new_correlation_id
from .utils import DEVICE_COOKIE, client_ip, device_id, user_agent

logger = logging.getLogger(__name__)


class HealthCheckMiddleware:
    """Answer liveness/readiness probes before host validation.

    Load-balancer health checks use the target's IP as the Host header and
    the image HEALTHCHECK uses 127.0.0.1 - neither is in a production
    ALLOWED_HOSTS, so CommonMiddleware would reject them with 400 and the
    orchestrator would keep replacing healthy containers. Must be first.
    """

    PATHS = ('/healthz/live', '/healthz/ready')

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path in self.PATHS and request.method == 'GET':
            from . import views

            return (views.liveness if request.path == self.PATHS[0] else views.readiness)(request)
        return self.get_response(request)

_REQUEST_ID_RE = re.compile(r'^[A-Za-z0-9._-]{8,64}$')


class CorrelationIdMiddleware:
    """Attach a request/correlation id to logs, audit events and responses."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get('X-Request-ID', '')
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else new_correlation_id()
        token = correlation_id_var.set(request_id)
        request.correlation_id = request_id
        try:
            response = self.get_response(request)
        finally:
            correlation_id_var.reset(token)
        response['X-Request-ID'] = request_id
        return response


class MetricsMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _time_query(execute, sql, params, many, context):
        start = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            metrics.DB_LATENCY.observe(time.perf_counter() - start)

    def __call__(self, request):
        start = time.perf_counter()
        with connection.execute_wrapper(self._time_query):
            response = self.get_response(request)
        match = getattr(request, 'resolver_match', None)
        view = (match.view_name if match else 'unresolved') or 'unresolved'
        metrics.HTTP_LATENCY.labels(view=view).observe(time.perf_counter() - start)
        metrics.HTTP_REQUESTS.labels(method=request.method, status_class=f'{response.status_code // 100}xx').inc()
        return response


# Endpoints that enqueue background work on every call; shed them first when
# workers fall behind instead of letting the backlog grow without bound.
BACKPRESSURE_PATHS = ('/api/v1/', '/payments/', '/e/')


class BackpressureMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method == 'POST' and request.path.startswith(BACKPRESSURE_PATHS) \
                and not request.path.startswith(('/api/v1/webhooks/', '/payments/webhook/')):
            depth = cache.get('fv:queue_depth_total')
            if depth is None:
                depth = sum(metrics.queue_depths().values())
                cache.set('fv:queue_depth_total', depth, 5)
            if depth > settings.QUEUE_BACKPRESSURE_THRESHOLD:
                logger.warning('Shedding load: queue depth %s', depth)
                response = JsonResponse({'error': {'code': 'overloaded',
                                                   'message': 'The service is busy. Please retry shortly.'}},
                                        status=503)
                response['Retry-After'] = '10'
                return response
        return self.get_response(request)


class ApiCorsMiddleware:
    """CORS only for /api/ and only for explicitly allow-listed origins."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        origin = request.headers.get('Origin')
        is_api = request.path.startswith('/api/')
        allowed = is_api and origin and origin in settings.API_CORS_ALLOWED_ORIGINS
        if is_api and request.method == 'OPTIONS' and 'Access-Control-Request-Method' in request.headers:
            response = HttpResponse(status=204 if allowed else 403)
        else:
            response = self.get_response(request)
        if allowed:
            response['Access-Control-Allow-Origin'] = origin
            response['Vary'] = 'Origin'
            response['Access-Control-Allow-Methods'] = 'GET, POST, PATCH, DELETE, OPTIONS'
            response['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, Idempotency-Key, X-Request-ID'
            response['Access-Control-Max-Age'] = '600'
        return response


MFA_EXEMPT_PREFIXES = ('/account/', '/logout/', '/login/', '/static/', '/healthz/', '/i18n/', '/prefs/')


class SessionSecurityMiddleware:
    """Tracks authenticated sessions (device/session management), honours
    remote revocation, and enforces MFA enrolment for privileged users."""

    TOUCH_INTERVAL = 300

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated and request.session.session_key:
            from .models import UserSession

            key = request.session.session_key
            tracked = UserSession.objects.filter(session_key=key).only('id', 'revoked', 'last_seen_at').first()
            if tracked is not None and tracked.revoked:
                logout(request)
                return redirect(settings.LOGIN_URL)
            now = timezone.now()
            if tracked is None:
                UserSession.objects.create(user=user, session_key=key, ip_address=client_ip(request),
                                           user_agent=user_agent(request))
            elif (now - tracked.last_seen_at).total_seconds() > self.TOUCH_INTERVAL:
                UserSession.objects.filter(pk=tracked.pk).update(last_seen_at=now, ip_address=client_ip(request))
            if self._mfa_required(user) and not request.path.startswith(MFA_EXEMPT_PREFIXES) \
                    and not request.path.startswith('/' + settings.ADMIN_URL + 'logout'):
                if request.path.startswith('/api/'):
                    return JsonResponse({'error': {'code': 'mfa_enrolment_required',
                                                   'message': 'Enrol a second factor before using the API.'}},
                                        status=403)
                return redirect('/account/security/?enrol=1')
        return self.get_response(request)

    @staticmethod
    def _mfa_required(user):
        security = getattr(user, 'security', None)
        forced = bool(security and security.mfa_enforced)
        if not (forced or settings.ENFORCE_STAFF_MFA):
            return False
        from .rbac import is_console_user

        if not forced and not is_console_user(user):
            return False
        return not (security and security.mfa_enabled)


PREF_FLAGS = {'hc', 'lg', 'lite'}


class UserPreferencesMiddleware:
    """Accessibility / low-bandwidth / timezone preferences + device cookie."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        raw = request.COOKIES.get('fv_prefs', '')
        request.prefs = {flag for flag in raw.split('.') if flag in PREF_FLAGS}
        tz_name = request.COOKIES.get('fv_tz', '')
        activated = False
        if tz_name:
            try:
                timezone.activate(zoneinfo.ZoneInfo(tz_name))
                activated = True
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                pass
        device_id(request)
        try:
            response = self.get_response(request)
        finally:
            if activated:
                timezone.deactivate()
        new_device = getattr(request, '_fv_new_device_id', None)
        if new_device:
            response.set_cookie(DEVICE_COOKIE, new_device, max_age=2 * 365 * 24 * 3600, httponly=True,
                                secure=not settings.DEBUG, samesite='Lax')
        return response


def build_csp(nonce):
    from .captcha import provider_config

    name, captcha = provider_config()
    captcha_hosts = []
    if captcha:
        if name == 'turnstile':
            captcha_hosts = ['https://challenges.cloudflare.com']
        elif name == 'hcaptcha':
            captcha_hosts = ['https://js.hcaptcha.com', 'https://*.hcaptcha.com']
        else:
            captcha_hosts = ['https://www.google.com', 'https://www.gstatic.com']
    cdn = ['https://cdn.jsdelivr.net', 'https://cdnjs.cloudflare.com', 'https://unpkg.com']
    directives = {
        'default-src': ["'self'"],
        'script-src': ["'self'", f"'nonce-{nonce}'", *cdn, *captcha_hosts, *settings.CSP_EXTRA_SCRIPT_SRC],
        'style-src': ["'self'", "'unsafe-inline'", 'https://fonts.googleapis.com', *cdn],
        'font-src': ["'self'", 'data:', 'https://fonts.gstatic.com', *cdn],
        'img-src': ["'self'", 'data:', 'blob:', 'https://res.cloudinary.com', 'https://images.unsplash.com',
                    'https://via.placeholder.com'],
        'connect-src': ["'self'", *captcha_hosts, *settings.CSP_EXTRA_CONNECT_SRC],
        'frame-src': ["'self'", *captcha_hosts],
        'media-src': ["'self'", 'blob:'],
        'worker-src': ["'self'", 'blob:'],
        'object-src': ["'none'"],
        'base-uri': ["'self'"],
        'frame-ancestors': ["'none'"],
        # Browsers apply form-action to the redirect that follows a POST, so
        # the payment gateway and identity providers must be listed.
        'form-action': ["'self'", 'https://checkout.paystack.com', 'https://accounts.google.com',
                        'https://login.microsoftonline.com'],
    }
    if not settings.DEBUG:
        directives['upgrade-insecure-requests'] = []
    return '; '.join(f"{k} {' '.join(v)}".strip() for k, v in directives.items())


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.csp_nonce = secrets.token_urlsafe(16)
        response = self.get_response(request)
        header = 'Content-Security-Policy-Report-Only' if settings.CSP_REPORT_ONLY else 'Content-Security-Policy'
        if header not in response:
            response[header] = build_csp(request.csp_nonce)
        response.setdefault('Permissions-Policy',
                            'camera=(self), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()')
        response.setdefault('Cross-Origin-Resource-Policy', 'same-origin')
        response.setdefault('X-Permitted-Cross-Domain-Policies', 'none')
        user = getattr(request, 'user', None)
        sensitive = request.path.startswith(('/e/', '/console/', '/account/', '/dashboard/', '/portal/', '/api/'))
        if sensitive or (user is not None and user.is_authenticated):
            response['Cache-Control'] = 'no-store, private'
        return response
