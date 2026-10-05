from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from api.api import api
from elections.urls import portal_patterns
from payments import views as payment_views
from voting import views as voting_views

handler403 = 'core.views.error_403'
handler404 = 'core.views.error_404'
handler500 = 'core.views.error_500'

admin.site.site_header = 'FlexyVotes administration'
admin.site.site_title = 'FlexyVotes admin'

urlpatterns = [
    path(settings.ADMIN_URL, admin.site.urls),
    path('api/v1/', api.urls),
    path('i18n/', include('django.conf.urls.i18n')),
    # Paystack and Africa's Talking are configured with these paths.
    path('webhook/paystack/', payment_views.webhook, name='paystack_webhook'),
    path('ussd/callback/', voting_views.ussd_callback, name='ussd_callback'),
    path('', include('core.urls')),
    path('', include('elections.urls')),
    path('', include('payments.urls')),
    path('', include('fraud.views')),
    path('', include('billing.views')),
    path('portal/', include(portal_patterns)),
    path('', include('voting.urls')),

    path('password_reset/', auth_views.PasswordResetView.as_view(template_name='voting/password_reset.html'),
         name='password_reset'),
    path('password_reset/done/', auth_views.PasswordResetDoneView.as_view(template_name='voting/password_reset_done.html'),
         name='password_reset_done'),
    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(template_name='voting/password_reset_confirm.html'),
         name='password_reset_confirm'),
    path('reset/done/', auth_views.PasswordResetCompleteView.as_view(template_name='voting/password_reset_complete.html'),
         name='password_reset_complete'),
    path('account/password/', auth_views.PasswordChangeView.as_view(template_name='account/password_change.html',
                                                                     success_url='/account/security/'),
         name='password_change'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
