from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.views.decorators.http import require_POST

from core.rbac import events_for_user, has_perm, is_platform_admin

from . import service
from .models import BlocklistEntry, FraudEvent


def _scoped(user):
    events = events_for_user(user, 'fraud.view')
    query = Q(event__in=events)
    if is_platform_admin(user):
        query |= Q(event__isnull=True)
    return FraudEvent.objects.filter(query)


@login_required
def alerts(request):
    queryset = _scoped(request.user).select_related('event', 'payment', 'candidate')
    if not (is_platform_admin(request.user) or events_for_user(request.user, 'fraud.view').exists()):
        raise PermissionDenied
    status = request.GET.get('status', 'OPEN')
    if status in FraudEvent.Status.values:
        queryset = queryset.filter(status=status)
    decision = request.GET.get('decision')
    if decision in FraudEvent.Decision.values:
        queryset = queryset.filter(decision=decision)
    kind = request.GET.get('kind')
    if kind in FraudEvent.Kind.values:
        queryset = queryset.filter(kind=kind)
    summary = dict(_scoped(request.user).filter(status=FraudEvent.Status.OPEN).values_list('decision').annotate(n=Count('pk')))
    return render(request, 'fraud/alerts.html', {
        'page': Paginator(queryset, 50).get_page(request.GET.get('page')), 'summary': summary, 'status': status,
        'decision': decision, 'kind': kind, 'statuses': FraudEvent.Status.choices,
        'decisions': FraudEvent.Decision.choices, 'kinds': FraudEvent.Kind.choices})


@login_required
def alert_detail(request, alert_id):
    alert = get_object_or_404(_scoped(request.user).select_related('event', 'payment', 'candidate', 'reviewed_by'), pk=alert_id)
    if request.method == 'POST':
        if not has_perm(request.user, 'fraud.review', alert.event):
            raise PermissionDenied
        try:
            service.review(alert, request.user, request.POST.get('outcome'), request.POST.get('notes', '')[:2000],
                           request, block=request.POST.get('block') == 'on')
            messages.success(request, 'Alert reviewed.')
        except service.FraudReviewError as exc:
            messages.error(request, str(exc))
        return redirect('fraud:alert', alert_id=alert.pk)
    related = FraudEvent.objects.none()
    if alert.device_hash:
        related = _scoped(request.user).filter(device_hash=alert.device_hash).exclude(pk=alert.pk)[:20]
    return render(request, 'fraud/alert_detail.html', {'alert': alert, 'related': related,
                                                       'can_review': has_perm(request.user, 'fraud.review', alert.event)})


@login_required
def blocklist(request):
    """Platform admins manage platform-wide and per-organization entries.
    Tenant fraud analysts only see and manage their own organizations'
    entries, which only affect their own events."""
    from core.models import Organization

    platform = is_platform_admin(request.user)
    organizations = Organization.objects.filter(
        pk__in=events_for_user(request.user, 'fraud.review').values('organization_id')).order_by('name')
    if not platform and not organizations.exists():
        raise PermissionDenied
    entries = BlocklistEntry.objects.filter(is_active=True).select_related('organization')
    if not platform:
        entries = entries.filter(organization__in=organizations)
    if request.method == 'POST':
        try:
            if request.POST.get('action') == 'deactivate':
                entry = get_object_or_404(entries, pk=request.POST.get('entry'))
                entry.is_active = False
                entry.save(update_fields=['is_active'])
                messages.success(request, 'Entry deactivated.')
            else:
                choice = request.POST.get('organization') or ''
                if platform and not choice:
                    organization = None  # platform-wide
                else:
                    pool = Organization.objects.all() if platform else organizations
                    organization = pool.filter(pk=choice).first() if choice else organizations.first()
                    if organization is None:
                        raise PermissionDenied
                service.add_block(request.POST.get('kind'), request.POST.get('value'), request.user,
                                  (request.POST.get('reason') or '')[:255], organization=organization)
                messages.success(request, 'Added to the blocklist.')
        except service.FraudReviewError as exc:
            messages.error(request, str(exc))
        return redirect('fraud:blocklist')
    return render(request, 'fraud/blocklist.html', {
        'entries': entries[:500], 'kinds': BlocklistEntry.Kind.choices, 'platform': platform,
        'organizations': Organization.objects.order_by('name') if platform else organizations})


app_name = 'fraud'
urlpatterns = [
    path('console/fraud/', alerts, name='alerts'),
    path('console/fraud/blocklist/', blocklist, name='blocklist'),
    path('console/fraud/<int:alert_id>/', alert_detail, name='alert'),
]
