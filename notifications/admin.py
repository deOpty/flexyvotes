from django.contrib import admin

from core.admin import ReadOnlyAdmin

from .models import Notification


@admin.register(Notification)
class NotificationAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'template', 'channel', 'recipient_hint', 'status', 'attempts')
    list_filter = ('channel', 'status', 'template')
    exclude = ('recipient', 'context')
