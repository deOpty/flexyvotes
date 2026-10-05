"""Fixed-window rate limiting backed by the shared cache (Redis in production,
so limits hold across every app instance)."""
import time
from functools import wraps

from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import render

from .utils import client_ip


def hit(scope, ident, limit, window=60):
    """Count one attempt. Returns (allowed, retry_after_seconds)."""
    now = time.time()
    bucket = int(now // window)
    key = f'rl:{scope}:{ident}:{bucket}'
    if cache.add(key, 1, window + 5):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, window + 5)
            count = 1
    retry_after = int((bucket + 1) * window - now) + 1
    return count <= limit, retry_after


def peek(scope, ident, window=60):
    bucket = int(time.time() // window)
    return cache.get(f'rl:{scope}:{ident}:{bucket}', 0)


def reset(scope, ident, window=60):
    bucket = int(time.time() // window)
    cache.delete(f'rl:{scope}:{ident}:{bucket}')


def too_many_requests(request, retry_after, message='Too many requests. Please wait a moment and try again.'):
    from . import metrics

    metrics.RATE_LIMITED.labels(scope=getattr(request, '_fv_ratelimit_scope', 'unknown')).inc()
    if request.path.startswith('/api/') or request.headers.get('X-Requested-With') == 'XMLHttpRequest' \
            or 'application/json' in request.headers.get('Accept', ''):
        response = JsonResponse({'error': {'code': 'rate_limited', 'message': message}}, status=429)
    else:
        response = render(request, 'core/429.html', {'message': message, 'retry_after': retry_after}, status=429)
    response['Retry-After'] = str(retry_after)
    return response


def ratelimit(scope, limit, window=60, key='ip', methods=('POST',)):
    """Decorator. ``key`` is 'ip', 'user', 'ip+user' or a callable(request)."""

    def identity(request):
        if callable(key):
            return key(request)
        parts = []
        if 'ip' in key:
            parts.append(client_ip(request) or 'unknown')
        if 'user' in key:
            parts.append(str(request.user.pk) if request.user.is_authenticated else 'anon')
        return ':'.join(parts)

    def decorator(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.method in methods:
                allowed, retry_after = hit(scope, identity(request), limit, window)
                if not allowed:
                    request._fv_ratelimit_scope = scope
                    return too_many_requests(request, retry_after)
            return view(request, *args, **kwargs)

        return wrapper

    return decorator
