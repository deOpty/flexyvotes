import zoneinfo
from decimal import Decimal, InvalidOperation

from babel.numbers import format_currency, format_decimal, format_percent
from django import template
from django.utils import timezone, translation
from django.utils.html import format_html

from core import captcha as captcha_module
from core import rbac

register = template.Library()


def _locale():
    return (translation.get_language() or 'en').replace('-', '_')


@register.filter
def money(value, currency='GHS'):
    try:
        amount = Decimal(str(value if value is not None else 0))
    except InvalidOperation:
        return value
    try:
        return format_currency(amount, currency or 'GHS', locale=_locale())
    except Exception:  # noqa: BLE001 - unknown locale/currency
        return f'{currency} {amount:,.2f}'


@register.filter
def number(value):
    try:
        return format_decimal(value or 0, locale=_locale())
    except Exception:  # noqa: BLE001
        return value


@register.filter
def pct(value, digits=1):
    """Format a 0-100 percentage."""
    try:
        return format_percent(float(value or 0) / 100, format=f"#,##0.{'0' * int(digits)}%", locale=_locale())
    except Exception:  # noqa: BLE001
        return f'{value}%'


@register.filter
def in_tz(value, tz_name):
    if not value:
        return value
    try:
        return timezone.localtime(value, zoneinfo.ZoneInfo(tz_name))
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return timezone.localtime(value)


@register.simple_tag
def can(user, perm, obj=None):
    return rbac.has_perm(user, perm, obj)


STATUS_CLASSES = {
    'DRAFT': 'secondary', 'REVIEW': 'warning text-dark', 'APPROVED': 'info text-dark', 'SCHEDULED': 'info text-dark',
    'OPEN': 'success', 'PAUSED': 'warning text-dark', 'CLOSED': 'dark', 'TALLYING': 'primary',
    'CERTIFIED': 'primary', 'PUBLISHED': 'success', 'ARCHIVED': 'secondary',
    'SUCCESS': 'success', 'PENDING': 'warning text-dark', 'INITIALIZED': 'secondary', 'FAILED': 'danger',
    'ABANDONED': 'secondary', 'REFUNDED': 'info text-dark', 'REVERSED': 'danger', 'DISPUTED': 'danger',
    'ELIGIBLE': 'secondary', 'VERIFIED': 'info text-dark', 'VOTED': 'success', 'SUSPENDED': 'danger',
    'INELIGIBLE': 'dark', 'OPEN_CASE': 'danger', 'HOLD': 'danger', 'CHALLENGE': 'warning text-dark',
    'MONITOR': 'info text-dark', 'ALLOW': 'success',
}


@register.filter
def status_badge(value, label=None):
    css = STATUS_CLASSES.get(str(value), 'secondary')
    return format_html('<span class="badge bg-{}">{}</span>', css, label or str(value).replace('_', ' ').title())


@register.inclusion_tag('core/includes/bot_protection.html', takes_context=True)
def bot_protection(context):
    name, config = captcha_module.provider_config()
    request = context.get('request')
    from django.conf import settings

    return {
        'stamp': captcha_module.form_stamp(),
        'honeypot': captcha_module.HONEYPOT_FIELD,
        'stamp_field': captcha_module.TIMESTAMP_FIELD,
        'captcha': config,
        'site_key': settings.CAPTCHA_SITE_KEY if config else '',
        'csp_nonce': getattr(request, 'csp_nonce', ''),
    }


@register.filter
def get_item(mapping, key):
    if mapping is None:
        return None
    return mapping.get(key) if hasattr(mapping, 'get') else None
