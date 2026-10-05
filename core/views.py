import time
import zoneinfo
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import crypto, metrics
from .rbac import is_platform_admin
from .utils import safe_next


def _check_database():
    start = time.perf_counter()
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1')
        cursor.fetchone()
    return {'ok': True, 'latency_ms': round((time.perf_counter() - start) * 1000, 2)}


def _check_cache():
    start = time.perf_counter()
    cache.set('fv:health', '1', 10)
    ok = cache.get('fv:health') == '1'
    return {'ok': ok, 'latency_ms': round((time.perf_counter() - start) * 1000, 2)}


def _check_broker():
    broker = settings.CELERY_BROKER_URL
    if settings.CELERY_TASK_ALWAYS_EAGER or not broker.startswith(('redis://', 'rediss://')):
        return {'ok': True, 'mode': 'eager (no broker configured)'}
    try:
        import redis

        start = time.perf_counter()
        redis.Redis.from_url(broker, socket_timeout=2, socket_connect_timeout=2).ping()
        return {'ok': True, 'latency_ms': round((time.perf_counter() - start) * 1000, 2),
                'queue_depth': metrics.queue_depths()}
    except Exception as exc:  # noqa: BLE001
        return {'ok': False, 'error': exc.__class__.__name__}


def _check_migrations():
    cached = cache.get('fv:health:migrations')
    if cached is not None:
        return cached
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
    result = {'ok': not pending, 'pending': len(pending)}
    cache.set('fv:health:migrations', result, 60)
    return result


def run_health_checks():
    checks = {}
    for name, check in (('database', _check_database), ('cache', _check_cache), ('broker', _check_broker),
                        ('migrations', _check_migrations)):
        try:
            checks[name] = check()
        except Exception as exc:  # noqa: BLE001
            checks[name] = {'ok': False, 'error': exc.__class__.__name__}
    return checks


@never_cache
@require_GET
def liveness(request):
    """Process is up and serving requests (no dependency checks)."""
    return JsonResponse({'status': 'alive', 'version': settings.APP_VERSION})


@never_cache
@require_GET
def readiness(request):
    """Ready to take traffic: database, cache, broker reachable, schema current."""
    checks = run_health_checks()
    ready = all(c['ok'] for c in checks.values())
    payload = {'status': 'ready' if ready else 'not_ready'}
    # Probes may arrive before authentication middleware runs (see
    # HealthCheckMiddleware), so there may be no request.user.
    token = settings.METRICS_TOKEN
    if is_platform_admin(getattr(request, 'user', None)) or (
            token and crypto.constant_time_equals(request.headers.get('Authorization', ''), f'Bearer {token}')):
        payload['checks'] = checks
    return JsonResponse(payload, status=200 if ready else 503)


@never_cache
@require_GET
def metrics_view(request):
    token = settings.METRICS_TOKEN
    authorized = is_platform_admin(request.user) or (
        token and crypto.constant_time_equals(request.headers.get('Authorization', ''), f'Bearer {token}')
    )
    if not authorized:
        return HttpResponseForbidden('Metrics require a bearer token.')
    body, content_type = metrics.render_latest()
    return HttpResponse(body, content_type=content_type)


@require_POST
def set_preferences(request):
    """Accessibility / low-bandwidth toggles (stored in a cookie, no login needed)."""
    flags = {flag for flag in ('hc', 'lg', 'lite') if request.POST.get(flag) == 'on'}
    response = redirect(safe_next(request, request.POST.get('next'), '/'))
    response.set_cookie('fv_prefs', '.'.join(sorted(flags)), max_age=365 * 24 * 3600, samesite='Lax',
                        secure=not settings.DEBUG, httponly=False)
    return response


@require_POST
def set_timezone(request):
    tz_name = request.POST.get('timezone', '')
    response = JsonResponse({'ok': True})
    try:
        zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return JsonResponse({'ok': False}, status=400)
    response.set_cookie('fv_tz', tz_name, max_age=365 * 24 * 3600, samesite='Lax', secure=not settings.DEBUG)
    return response


@require_GET
def accessibility_page(request):
    return render(request, 'core/accessibility.html', {'next': safe_next(request, request.GET.get('next'), '/')})


@require_GET
def signing_key(request):
    """Public verification keys for signed configurations and results."""
    current = crypto.public_key_b64()
    return JsonResponse({
        'algorithm': 'Ed25519',
        'current': {'public_key': current, 'key_id': crypto.key_fingerprint(current)},
        'previous': [{'public_key': k, 'key_id': crypto.key_fingerprint(k)} for k in settings.SIGNING_PREVIOUS_PUBLIC_KEYS],
    })


@require_GET
def security_txt(request):
    # RFC 9116: Expires should be less than a year ahead; keep it rolling.
    expires = (timezone.now() + timedelta(days=180)).strftime('%Y-%m-%dT00:00:00.000Z')
    lines = [
        f'Contact: mailto:{settings.SECURITY_CONTACT}',
        f'Expires: {expires}',
        'Preferred-Languages: en, fr',
        f'Canonical: {settings.SITE_URL}/.well-known/security.txt',
        'Policy: Please report vulnerabilities privately; do not test against live elections.',
    ]
    return HttpResponse('\n'.join(lines) + '\n', content_type='text/plain')


def error_403(request, exception=None):
    return render(request, 'core/403.html', status=403)


def error_404(request, exception=None):
    return render(request, 'core/404.html', status=404)


def error_500(request):
    return render(request, 'core/500.html', {'correlation_id': getattr(request, 'correlation_id', '')}, status=500)
