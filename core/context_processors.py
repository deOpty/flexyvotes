from django.conf import settings

from .captcha import provider_config


def platform(request):
    user = getattr(request, 'user', None)
    console = False
    platform_admin = False
    if user is not None and user.is_authenticated:
        from .rbac import is_console_user, is_platform_admin

        console = is_console_user(user)
        platform_admin = is_platform_admin(user)
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
        'captcha_provider': captcha_name,
        'captcha': captcha,
        'captcha_site_key': settings.CAPTCHA_SITE_KEY if captcha else '',
        'APP_VERSION': settings.APP_VERSION,
    }
