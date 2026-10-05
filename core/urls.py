from django.urls import include, path
from django.views.generic import RedirectView

from . import views, views_account as account, views_console as console

account_patterns = ([
    path('security/', account.security, name='security'),
    path('security/action/', account.security_action, name='security_action'),
    path('passkeys/options/', account.passkey_register_options, name='passkey_options'),
    path('passkeys/register/', account.passkey_register, name='passkey_register'),
    path('mfa/', account.mfa, name='mfa'),
    path('mfa/passkey/options/', account.mfa_passkey_options, name='mfa_passkey_options'),
    path('mfa/passkey/verify/', account.mfa_passkey_verify, name='mfa_passkey_verify'),
], 'account')

console_patterns = ([
    path('', RedirectView.as_view(pattern_name='dashboard', permanent=False), name='home'),
    path('approvals/', console.approvals, name='approvals'),
    path('audit/', console.audit_log, name='audit'),
    path('team/', console.team, name='team'),
    path('organizations/', console.organizations, name='organizations'),
    path('organizations/<int:org_id>/', console.organization_settings, name='organization'),
    path('organizations/<int:org_id>/team/', console.team, name='team_org'),
    path('organizers/', console.organizers, name='organizers'),
    path('health/', console.health, name='health'),
    path('support/', console.support, name='support'),
    path('support/<int:ticket_id>/', console.support_ticket, name='support_ticket'),
    path('notifications/', console.notifications_view, name='notifications'),
    path('reports/', console.reports, name='reports'),
    path('switch-organization/', console.switch_organization, name='switch_org'),
], 'console')

urlpatterns = [
    path('healthz/live', views.liveness, name='healthz_live'),
    path('healthz/ready', views.readiness, name='healthz_ready'),
    path('metrics', views.metrics_view, name='metrics'),
    path('prefs/', views.set_preferences, name='set_preferences'),
    path('prefs/timezone/', views.set_timezone, name='set_timezone'),
    path('accessibility/', views.accessibility_page, name='accessibility'),
    path('.well-known/security.txt', views.security_txt, name='security_txt'),
    path('.well-known/flexyvotes-signing-key.json', views.signing_key, name='signing_key'),
    path('auth/sso/<str:provider>/start/', account.sso_start, name='sso_start'),
    path('auth/sso/<str:provider>/callback/', account.sso_callback, name='sso_callback'),
    path('dashboard/', console.dashboard, name='dashboard'),
    path('account/', include(account_patterns)),
    path('console/', include(console_patterns)),
]
