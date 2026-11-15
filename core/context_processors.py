from django.conf import settings

from .captcha import provider_config


def _shows_approvals(perms):
    from .rbac import APPROVAL_QUEUE_PERMISSIONS

    return bool(perms & APPROVAL_QUEUE_PERMISSIONS)


def _pending_access_requests():
    from .models import AccessRequest

    return AccessRequest.objects.filter(status=AccessRequest.Status.PENDING).count()


def platform(request):
    user = getattr(request, 'user', None)
    console = False
    platform_admin = False
    nav_perms = frozenset()
    if user is not None and user.is_authenticated:
        from .rbac import is_console_user, is_platform_admin, permissions_anywhere

        console = is_console_user(user)
        platform_admin = is_platform_admin(user)
        if console:
            # Console menus show only what the user can actually use.
            nav_perms = permissions_anywhere(user)
    captcha_name, captcha = provider_config()
    prefs = getattr(request, 'prefs', set())
    return {
        'PLATFORM_NAME': settings.PLATFORM_NAME,
        'SITE_URL': settings.SITE_URL,
        'csp_nonce': getattr(request, 'csp_nonce', ''),
        'prefs': prefs,
        'lite_mode': 'lite' in prefs,
        'is_console_user': console,
        'is_platform_admin': platform_admin,
        'nav_perms': nav_perms,
        'nav_approvals': _shows_approvals(nav_perms),
        # Callable: the template only runs the query where it shows the badge.
        'pending_access_requests': _pending_access_requests if platform_admin else 0,
        'captcha_provider': captcha_name,
        'captcha': captcha,
        'captcha_site_key': settings.CAPTCHA_SITE_KEY if captcha else '',
        'APP_VERSION': settings.APP_VERSION,
    }
