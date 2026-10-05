from django.apps import apps
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.db.models.signals import post_migrate
from django.dispatch import receiver
from django.utils import timezone

from . import audit, metrics
from .utils import client_ip, user_agent


@receiver(post_migrate)
def sync_system_roles(sender, **kwargs):
    """Keep system roles' permission bundles in sync with core.rbac."""
    if sender.name != 'core':
        return
    from .rbac import ROLE_DEFINITIONS

    Role = apps.get_model('core', 'Role')
    for code, (name, description, permissions) in ROLE_DEFINITIONS.items():
        Role.objects.update_or_create(code=code, defaults={
            'name': name, 'description': description, 'permissions': sorted(permissions), 'is_system': True,
        })


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    from .models import UserSecurity, UserSession

    metrics.LOGINS.labels(kind='staff', outcome='success').inc()
    if request is None:
        return
    if request.session.session_key:
        UserSession.objects.update_or_create(session_key=request.session.session_key, defaults={
            'user': user, 'ip_address': client_ip(request), 'user_agent': user_agent(request),
        })
    UserSecurity.objects.update_or_create(user=user, defaults={
        'failed_login_count': 0, 'locked_until': None, 'last_login_ip': client_ip(request),
    })
    audit.record('AUTH_LOGIN', request=request, actor=user, target=user, summary='Signed in')


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    from .models import UserSession

    if request is not None and request.session.session_key:
        UserSession.objects.filter(session_key=request.session.session_key).update(ended_at=timezone.now())
    if user is not None:
        audit.record('AUTH_LOGOUT', request=request, actor=user, target=user, summary='Signed out')


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request=None, **kwargs):
    metrics.LOGINS.labels(kind='staff', outcome='failure').inc()
    audit.record('AUTH_LOGIN_FAILED', request=request, result='FAILURE',
                 summary=f"Failed sign-in for '{(credentials or {}).get('username', '')[:80]}'")
