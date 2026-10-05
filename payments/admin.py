from django.contrib import admin

from core.admin import ReadOnlyAdmin

from .models import DiscountCode, Payment, PaymentEvent, ReconciliationRun, Refund, VotePackage, WebhookEvent


@admin.register(Payment)
class PaymentAdmin(ReadOnlyAdmin):
    list_display = ('reference', 'event', 'candidate', 'amount', 'currency', 'status', 'votes_credited', 'held', 'created_at')
    list_filter = ('status', 'held', 'votes_credited', 'purpose')
    search_fields = ('reference',)
    exclude = ('payer_email', 'payer_phone', 'payer_email_index', 'payer_phone_index', 'device_hash')


admin.site.register(PaymentEvent, ReadOnlyAdmin)
admin.site.register(WebhookEvent, ReadOnlyAdmin)
admin.site.register(Refund, ReadOnlyAdmin)
admin.site.register(ReconciliationRun, ReadOnlyAdmin)
admin.site.register(VotePackage)
admin.site.register(DiscountCode)
