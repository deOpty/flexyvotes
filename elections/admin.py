from django.contrib import admin

from core.admin import ReadOnlyAdmin

from .models import (ApprovalRequest, Ballot, Constituency, Dispute, ElectionConfigSnapshot, ElectionResult,
                     EligibilityRule, EvidenceItem, Incident, ResultCertification, Voter)


@admin.register(Constituency)
class ConstituencyAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'kind', 'organization', 'parent')
    list_filter = ('kind', 'organization')
    search_fields = ('name', 'code')
    readonly_fields = ('path',)


@admin.register(Voter)
class VoterAdmin(admin.ModelAdmin):
    list_display = ('identifier', 'election', 'status', 'constituency', 'voted_at')
    list_filter = ('status', 'election')
    search_fields = ('identifier',)
    # Credentials and encrypted PII are never shown or edited here.
    exclude = ('credential_hash', 'credential_ciphertext', 'email', 'phone', 'email_index', 'phone_index',
               'sso_subject_index')
    readonly_fields = ('status', 'voted_at', 'credential_issued_at')

    def has_delete_permission(self, request, obj=None):
        return obj is None or obj.status != Voter.Status.VOTED


@admin.register(Ballot)
class BallotAdmin(ReadOnlyAdmin):
    list_display = ('tracker', 'election')
    exclude = ('ciphertext',)


admin.site.register(EligibilityRule)
admin.site.register(ElectionConfigSnapshot, ReadOnlyAdmin)
admin.site.register(ElectionResult, ReadOnlyAdmin)
admin.site.register(ResultCertification, ReadOnlyAdmin)
admin.site.register(ApprovalRequest, ReadOnlyAdmin)
admin.site.register(EvidenceItem, ReadOnlyAdmin)


@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    list_display = ('reference', 'election', 'category', 'status', 'created_at')
    list_filter = ('status', 'category')
    exclude = ('filer_email',)


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ('reference', 'title', 'severity', 'status', 'created_at')
    list_filter = ('severity', 'status')
