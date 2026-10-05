from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.models import User

from core import audit

from .models import Candidate, Category, Event, Product, ProductCategory, Profile, Ticket, TicketPurchase, VoteTransaction


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'event', 'ballot_type', 'min_select', 'max_select', 'seats', 'allow_abstain')
    list_filter = ('ballot_type', 'event')


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ('name', 'event', 'category', 'status', 'nominee_code')
    list_filter = ('status', 'event')
    search_fields = ('name', 'nominee_code')


@admin.register(VoteTransaction)
class VoteTransactionAdmin(admin.ModelAdmin):
    list_display = ('paystack_reference', 'candidate', 'number_of_votes', 'amount', 'status', 'vote_type', 'created_at')
    list_filter = ('status', 'vote_type')
    search_fields = ('paystack_reference',)
    readonly_fields = [f.name for f in VoteTransaction._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


admin.site.register(Ticket)
admin.site.register(TicketPurchase)
admin.site.register(ProductCategory)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'category', 'price', 'is_active')
    list_filter = ('is_active', 'category')


class ProfileInline(admin.StackedInline):
    model = Profile
    can_delete = False


class CustomUserAdmin(UserAdmin):
    inlines = (ProfileInline,)
    list_display = ('username', 'email', 'is_staff', 'is_approved_organizer')
    actions = ['approve_organizers', 'unapprove_organizers']

    @admin.display(boolean=True, description='Approved organizer')
    def is_approved_organizer(self, obj):
        return hasattr(obj, 'profile') and obj.profile.is_approved_organizer

    @admin.action(description='Approve selected as organizers')
    def approve_organizers(self, request, queryset):
        from core.tenancy import ensure_personal_organization
        from notifications.service import notify_user

        updated = 0
        for user in queryset:
            profile, _ = Profile.objects.get_or_create(user=user)
            if not profile.is_approved_organizer:
                profile.is_approved_organizer = True
                profile.save()
                ensure_personal_organization(user, actor=request.user)
                notify_user(user, 'organizer_approved', {'username': user.username})
                audit.record('ORGANIZER_APPROVED', request=request, target=user, summary=f'{user.username} approved (admin)')
                updated += 1
        self.message_user(request, f'{updated} user(s) approved as organizers and notified.')

    @admin.action(description='Unapprove selected organizers')
    def unapprove_organizers(self, request, queryset):
        updated = Profile.objects.filter(user__in=queryset, is_approved_organizer=True).update(is_approved_organizer=False)
        self.message_user(request, f'{updated} user(s) have been unapproved.')


admin.site.unregister(User)
admin.site.register(User, CustomUserAdmin)


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ('title', 'organization', 'voting_mode', 'status', 'is_active', 'start_date', 'end_date')
    list_filter = ('status', 'voting_mode', 'is_active')
    search_fields = ('title',)
    readonly_fields = ('status', 'opened_at', 'closed_at', 'certified_at', 'published_at', 'archived_at', 'submitted_by',
                       'reviewed_by')
    actions = ['approve_events']

    @admin.action(description='Approve selected elections (REVIEW -> APPROVED)')
    def approve_events(self, request, queryset):
        from elections.lifecycle import LifecycleError, transition

        approved = 0
        for event in queryset.filter(status=Event.Status.REVIEW):
            try:
                transition(event, 'approve', actor=request.user, request=request, reason='Approved in Django admin')
                approved += 1
            except LifecycleError as exc:
                self.message_user(request, f'{event.title}: {exc}', level='error')
        self.message_user(request, f'{approved} election(s) approved.')
