from django.conf import settings

from .captcha import provider_config


# Console pages about one election that live outside the elections namespace. The sidebar keeps
# "Elections" highlighted on them, as it does on the election tabs.
ELECTION_PAGES = frozenset({
    'edit_event', 'edit_category', 'edit_candidate', 'event_analytics', 'event_guestlist', 'event_scanner',
    'create_ticket', 'edit_ticket', 'payments:pricing',
})


def _is_election_page(request):
    match = getattr(request, 'resolver_match', None)
    view_name = match.view_name if match else ''
    return view_name.startswith('elections:') or view_name in ELECTION_PAGES


def _secure_cookies_blocked(request):
    """COOKIE_SECURE=True on a page the browser does not treat as secure
    (plain http:// on an IP or domain): the CSRF and session cookies are
    dropped, so the login form cannot work. The login and CSRF error pages
    explain this instead of failing silently."""
    if getattr(settings, 'COOKIE_SECURE_MODE', 'auto') != 'always':
        return False
    from .middleware import trustworthy_origin

    return not trustworthy_origin(request)


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
        'secure_cookies_blocked': _secure_cookies_blocked(request),
        'prefs': prefs,
        'lite_mode': 'lite' in prefs,
        'is_console_user': console,
        # Layout for pages shared by staff and the public (account security, 403/404): console users
        # keep the console menu, everyone else gets the public page. Both expose {% block console %}.
        'page_base': 'console/base.html' if console else 'core/page.html',
        'is_platform_admin': platform_admin,
        'nav_perms': nav_perms,
        'nav_election_page': console and _is_election_page(request),
        'nav_approvals': _shows_approvals(nav_perms),
        # Callable: the template only runs the query where it shows the badge.
        'pending_access_requests': _pending_access_requests if platform_admin else 0,
        'captcha_provider': captcha_name,
        'captcha': captcha,
        'captcha_site_key': settings.CAPTCHA_SITE_KEY if captcha else '',
        'APP_VERSION': settings.APP_VERSION,
    }
