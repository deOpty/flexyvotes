from django.contrib import admin

from .models import (ApiToken, AuditEvent, DataKey, KnownDevice, Organization, Role, RoleAssignment, SupportTicket,
                     UserSession)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'kind', 'is_personal', 'is_active', 'created_at')
    list_filter = ('kind', 'is_personal', 'is_active')
    search_fields = ('name', 'slug')
    exclude = ('sso_config', 'ldap_config', 'directory_config')


@admin.register(Role)
class RoleAdmin(ReadOnlyAdmin):
    list_display = ('code', 'name', 'is_system')


@admin.register(RoleAssignment)
class RoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ('user', 'role', 'organization', 'event', 'created_at')
    list_filter = ('role',)
    search_fields = ('user__username',)
    autocomplete_fields = ()


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ('created_at', 'event_type', 'actor_label', 'election_id', 'result', 'summary')
    list_filter = ('result', 'chain')
    search_fields = ('event_type', 'actor_label', 'summary', 'correlation_id')


@admin.register(DataKey)
class DataKeyAdmin(ReadOnlyAdmin):
    list_display = ('pk', 'purpose', 'kek_id', 'provider', 'is_active', 'created_at')
    exclude = ('wrapped_key',)


@admin.register(UserSession)
class UserSessionAdmin(ReadOnlyAdmin):
    list_display = ('user', 'ip_address', 'created_at', 'last_seen_at', 'ended_at', 'revoked')
    exclude = ('session_key',)


admin.site.register(KnownDevice, ReadOnlyAdmin)


@admin.register(ApiToken)
class ApiTokenAdmin(ReadOnlyAdmin):
    list_display = ('name', 'user', 'prefix', 'created_at', 'last_used_at', 'expires_at', 'revoked_at')
    exclude = ('token_hash',)


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ('reference', 'subject', 'requester_email', 'status', 'priority', 'created_at')
    list_filter = ('status', 'priority')
    search_fields = ('reference', 'subject', 'requester_email')
