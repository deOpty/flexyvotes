from django.contrib import admin

from .models import Coupon, FeatureFlag, FeatureOverride, Invoice, InvoiceLine, Plan, Subscription, UsageRecord


class InvoiceLineInline(admin.TabularInline):
    model = InvoiceLine
    extra = 0


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('number', 'organization', 'status', 'total', 'currency', 'issued_at', 'paid_at')
    list_filter = ('status',)
    inlines = [InvoiceLineInline]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ('organization', 'plan', 'status', 'billing_cycle', 'current_period_end')
    list_filter = ('status', 'plan')


admin.site.register(Plan)
admin.site.register(Coupon)
admin.site.register(FeatureFlag)
admin.site.register(FeatureOverride)
admin.site.register(UsageRecord)
