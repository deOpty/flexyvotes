from django.urls import path

from . import views

app_name = 'payments'

urlpatterns = [
    path('e/<int:event_id>/pay/<int:candidate_id>/', views.pay, name='pay'),
    path('e/<int:event_id>/pay/<int:candidate_id>/quote/', views.quote_view, name='quote'),
    path('payments/callback/', views.callback, name='callback'),
    path('payments/receipt/<str:reference>/', views.receipt, name='receipt'),
    path('payments/webhook/', views.webhook, name='webhook'),
    path('payments/simulator/<str:reference>/', views.simulator, name='simulator'),
    path('console/payments/', views.payments_list, name='console_list'),
    path('console/payments/reconciliation/', views.reconciliation, name='reconciliation'),
    path('console/payments/revenue/', views.revenue, name='revenue'),
    path('console/payments/refunds/', views.refunds, name='refunds'),
    path('console/payments/<str:reference>/', views.payment_detail, name='console_detail'),
    path('console/elections/<int:event_id>/pricing/', views.pricing, name='pricing'),
]
