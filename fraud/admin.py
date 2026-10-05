from django.contrib import admin

from .models import BlocklistEntry, FraudEvent


@admin.register(FraudEvent)
class FraudEventAdmin(admin.ModelAdmin):
    list_display = ('created_at', 'kind', 'score', 'decision', 'status', 'event')
    list_filter = ('kind', 'decision', 'status')
    readonly_fields = [f.name for f in FraudEvent._meta.fields if f.name not in ('status', 'notes')]


@admin.register(BlocklistEntry)
class BlocklistEntryAdmin(admin.ModelAdmin):
    list_display = ('kind', 'value', 'reason', 'is_active', 'expires_at')
    list_filter = ('kind', 'is_active')
