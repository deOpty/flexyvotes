"""Election management console (organizers, officials, auditors)."""
import csv
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMinute
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import audit, crypto
from core.models import AuditEvent, Organization
from core.rbac import check_perm, event_permission_required, events_for_user, has_perm, user_permissions
from voting.models import Candidate, Category, Event, VoteTransaction
from voting.views import sanitize_csv_value

from . import integrity, keys, lifecycle, results as results_service, voters as voter_service
from .ballot import ballot_definition, configuration_problems, rules_text
from .models import (ApprovalRequest, CaseNote, Constituency, Dispute, EligibilityRule, ElectionResult, EvidenceItem,
                     Incident, Recount, TrusteeShare, Voter)


def _base_context(request, event, tab):
    return {'event': event, 'tab': tab, 'perms': user_permissions(request.user, event),
            'actions': lifecycle.available_actions(event, request.user)}


@login_required
def election_list(request):
    events = events_for_user(request.user, 'election.view').select_related('organization').order_by('-created_at')
    status = request.GET.get('status')
    if status in Event.Status.values:
        events = events.filter(status=status)
    query = (request.GET.get('q') or '').strip()
    if query:
        events = events.filter(title__icontains=query)
    page = Paginator(events, 25).get_page(request.GET.get('page'))
    return render(request, 'console/elections/list.html', {'page': page, 'statuses': Event.Status.choices,
                                                           'status': status, 'query': query})


@event_permission_required('election.view')
def overview(request, event):
    event = lifecycle.tick(event)
    context = _base_context(request, event, 'overview')
    context['problems'] = configuration_problems(event) if event.status in (Event.Status.DRAFT, Event.Status.REVIEW,
                                                                             Event.Status.APPROVED) else []
    if event.is_institutional:
        context['turnout'] = voter_service.turnout(event)
    else:
        from payments.models import Payment

        context['paid'] = VoteTransaction.objects.filter(candidate__event=event, status='Success').aggregate(
            votes=Sum('number_of_votes'), revenue=Sum('amount'))
        context['held_payments'] = Payment.objects.filter(event=event, held=True, status='SUCCESS').count()
    context['ballot_key'] = getattr(event, 'ballot_key', None) if hasattr(event, 'ballot_key') else None
    context['recent_audit'] = AuditEvent.objects.filter(election_id=event.pk)[:10]
    context['pending_approvals'] = event.approval_requests.filter(status=ApprovalRequest.Status.PENDING)
    context['open_disputes'] = event.disputes.filter(status__in=[Dispute.Status.OPEN, Dispute.Status.UNDER_REVIEW]).count()
    context['position_count'] = event.categories.count()
    context['candidate_count'] = event.candidates.count()
    return render(request, 'console/elections/overview.html', context)


@login_required
@require_POST
def do_transition(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    action = request.POST.get('action', '')
    try:
        lifecycle.transition(event, action, actor=request.user, request=request,
                             reason=(request.POST.get('reason') or '')[:500])
        messages.success(request, f'{lifecycle.ACTIONS[action].label}: done.')
    except PermissionDenied:
        messages.error(request, 'You do not have permission to do that.')
    except (lifecycle.LifecycleError, keys.KeyCustodyError, KeyError) as exc:
        messages.error(request, str(exc) or 'That action is not available.')
    return redirect('elections:console_overview', event_id=event.pk)


SETTINGS_FEATURES = {'SSO': 'sso', 'LDAP': 'ldap'}


@event_permission_required('election.edit')
def settings_view(request, event):
    from billing.service import feature_enabled

    context = _base_context(request, event, 'settings')
    context.update({'auth_choices': Event.AUTH_METHOD_CHOICES, 'visibility_choices': Event.ResultsVisibility.choices,
                    'custody_choices': Event.KeyCustody.choices})
    allowed, resets, reason = lifecycle.edit_policy(event, 'config')
    context.update({'editable': allowed, 'resets': resets, 'edit_reason': reason})
    if request.method == 'POST':
        if not allowed:
            messages.error(request, reason)
            return redirect('elections:console_settings', event_id=event.pk)
        fields = ['auth_methods', 'require_second_factor', 'allow_self_registration', 'registration_email_domains',
                  'results_visibility', 'dual_approval_required', 'key_custody', 'trustee_threshold',
                  'record_constituency_on_ballot', 'min_anonymity_set', 'max_votes_per_voter',
                  'min_votes_per_transaction', 'max_votes_per_transaction', 'max_spend_per_voter',
                  'payment_channels', 'allowed_countries', 'is_active']
        before = audit.snapshot(event, fields)
        methods = [m for m in request.POST.getlist('auth_methods') if m in dict(Event.AUTH_METHOD_CHOICES)]
        for method, feature in SETTINGS_FEATURES.items():
            if method in methods and event.organization_id and not feature_enabled(event.organization, feature):
                messages.error(request, f'{method} sign-in requires a plan with the "{feature}" feature.')
                methods.remove(method)
        if event.is_institutional and not methods:
            methods = ['CODE']
        custody = request.POST.get('key_custody', event.key_custody)
        if custody == Event.KeyCustody.TRUSTEES and event.organization_id and \
                not feature_enabled(event.organization, 'trustee_keys'):
            messages.error(request, 'Trustee key custody requires the Government / High Assurance plan.')
            custody = Event.KeyCustody.SYSTEM
        if hasattr(event, 'ballot_key') and custody != event.key_custody:
            messages.error(request, 'Key custody cannot change after the ballot key exists.')
            custody = event.key_custody

        def _int(name, default=None, minimum=0):
            value = (request.POST.get(name) or '').strip()
            if value == '':
                return default
            try:
                return max(minimum, int(value))
            except ValueError:
                return default

        from voting.views import parse_non_negative_decimal

        event.auth_methods = methods
        event.require_second_factor = request.POST.get('require_second_factor') == 'on'
        event.allow_self_registration = request.POST.get('allow_self_registration') == 'on'
        event.registration_email_domains = (request.POST.get('registration_email_domains') or '')[:300]
        if request.POST.get('results_visibility') in Event.ResultsVisibility.values:
            event.results_visibility = request.POST['results_visibility']
        if event.is_institutional and event.results_visibility != Event.ResultsVisibility.AFTER_PUBLISH:
            event.results_visibility = Event.ResultsVisibility.AFTER_PUBLISH
            messages.info(request, 'Secret-ballot elections only reveal results after certification.')
        event.dual_approval_required = request.POST.get('dual_approval_required') == 'on'
        event.key_custody = custody
        event.trustee_threshold = _int('trustee_threshold', event.trustee_threshold, 0)
        event.record_constituency_on_ballot = request.POST.get('record_constituency_on_ballot') == 'on'
        event.min_anonymity_set = max(_int('min_anonymity_set', 5, 1), 1)
        event.max_votes_per_voter = _int('max_votes_per_voter')
        event.min_votes_per_transaction = max(_int('min_votes_per_transaction', 1, 1), 1)
        event.max_votes_per_transaction = _int('max_votes_per_transaction')
        event.max_spend_per_voter, _ = parse_non_negative_decimal(request.POST.get('max_spend_per_voter'), 'Spend', required=False)
        event.payment_channels = [c for c in request.POST.getlist('payment_channels')
                                  if c in ('card', 'bank', 'ussd', 'qr', 'mobile_money', 'bank_transfer', 'eft', 'apple_pay')]
        event.allowed_countries = [c.strip().upper()[:2] for c in (request.POST.get('allowed_countries') or '').split(',') if c.strip()]
        event.is_active = request.POST.get('is_active') == 'on'
        try:
            lifecycle.require_editable(event, 'config', request.user, request)
        except lifecycle.LifecycleError as exc:
            messages.error(request, str(exc))
            return redirect('elections:console_settings', event_id=event.pk)
        event.save()
        audit.record('ELECTION_CONFIG_UPDATED', request=request, event=event, summary='Election settings updated',
                     changes=audit.diff(before, audit.snapshot(event, fields)))
        messages.success(request, 'Settings saved.')
        return redirect('elections:console_settings', event_id=event.pk)
    context['payment_channel_choices'] = [('card', 'Card'), ('mobile_money', 'Mobile money'), ('bank', 'Bank'),
                                          ('ussd', 'USSD'), ('bank_transfer', 'Bank transfer'), ('qr', 'QR')]
    return render(request, 'console/elections/settings.html', context)


@event_permission_required('election.view')
def ballot(request, event):
    context = _base_context(request, event, 'ballot')
    context.update({
        'positions': event.categories.prefetch_related('candidates').select_related('constituency'),
        'uncategorized': event.candidates.filter(category__isnull=True),
        'ballot_types': Category.BallotType.choices,
        'constituencies': Constituency.objects.filter(organization_id=event.organization_id) if event.organization_id else [],
        'ballot_policy': lifecycle.edit_policy(event, 'ballot'),
        'candidate_policy': lifecycle.edit_policy(event, 'candidates'),
    })
    return render(request, 'console/elections/ballot.html', context)


@event_permission_required('election.view')
def ballot_preview(request, event):
    definition = ballot_definition(event)
    for position in definition:
        position['rules'] = rules_text(position)
    return render(request, 'vote/ballot.html', {'event': event, 'positions': definition, 'selections': {},
                                                'errors': {}, 'preview': True})


@event_permission_required('voter.view')
def voters(request, event):
    context = _base_context(request, event, 'voters')
    queryset = event.voters.select_related('constituency').order_by('identifier', 'pk')
    status = request.GET.get('status')
    if status in Voter.Status.values:
        queryset = queryset.filter(status=status)
    query = (request.GET.get('q') or '').strip()
    if query:
        if '@' in query:
            queryset = queryset.filter(email_index=crypto.blind_index(query, 'email'))
        else:
            queryset = queryset.filter(identifier__icontains=query.upper())
    context.update({'page': Paginator(queryset, 50).get_page(request.GET.get('page')), 'status': status, 'query': query,
                    'statuses': Voter.Status.choices, 'turnout': voter_service.turnout(event),
                    'voter_policy': lifecycle.edit_policy(event, 'voters'),
                    'constituencies': Constituency.objects.filter(organization_id=event.organization_id) if event.organization_id else []})
    return render(request, 'console/elections/voters.html', context)


@event_permission_required('voter.edit')
@require_POST
def voter_add(request, event):
    constituency = Constituency.objects.filter(organization_id=event.organization_id,
                                               pk=request.POST.get('constituency') or 0).first() if event.organization_id else None
    try:
        voter = voter_service.add_voter(event, request.user, identifier=request.POST.get('identifier') or None,
                                        full_name=request.POST.get('full_name', ''), email=request.POST.get('email') or None,
                                        phone=request.POST.get('phone') or None, constituency=constituency, request=request)
        messages.success(request, f'Voter {voter.identifier or voter.pk} added.')
    except (voter_service.VoterImportError, lifecycle.LifecycleError) as exc:
        messages.error(request, str(exc))
    except Exception as exc:  # noqa: BLE001 - plan limits
        if isinstance(exc, PermissionDenied):
            raise
        messages.error(request, str(exc))
    return redirect('elections:console_voters', event_id=event.pk)


@login_required
@require_POST
def voter_action(request, event_id, voter_id):
    event = get_object_or_404(Event, pk=event_id)
    voter = get_object_or_404(Voter, pk=voter_id, election=event)
    action = request.POST.get('action')
    reason = (request.POST.get('reason') or '')[:255]
    try:
        if action == 'reset':
            code = voter_service.reset_credential(voter, request.user, request, notify_voter=request.POST.get('notify') == 'on')
            messages.success(request, f'New access code for {voter.identifier or voter.pk}: {code} (shown once).')
        elif action in ('suspend', 'reinstate', 'ineligible', 'verify'):
            status = {'suspend': Voter.Status.SUSPENDED, 'reinstate': Voter.Status.ELIGIBLE,
                      'ineligible': Voter.Status.INELIGIBLE, 'verify': Voter.Status.VERIFIED}[action]
            voter_service.set_status(voter, status, request.user, reason, request)
            messages.success(request, 'Voter status updated.')
        elif action == 'invite':
            voter_service.send_invitations(event, request.user, Voter.objects.filter(pk=voter.pk), request=request)
            messages.success(request, 'Invitation queued.')
        elif action == 'remove':
            voter_service.remove_voter(voter, request.user, request)
            messages.success(request, 'Voter removed.')
        else:
            messages.error(request, 'Unknown action.')
    except (voter_service.VoterImportError, lifecycle.LifecycleError) as exc:
        messages.error(request, str(exc))
    return redirect('elections:console_voters', event_id=event.pk)


@event_permission_required('voter.credentials')
@require_POST
def voters_bulk(request, event):
    action = request.POST.get('action')
    try:
        if action == 'invite_all':
            sent = voter_service.send_invitations(event, request.user, request=request)
            messages.success(request, f'{sent} invitations queued.')
        elif action == 'generate_codes':
            created = voter_service.generate_anonymous_codes(event, request.POST.get('count') or 0, request.user, request)
            messages.success(request, f'{created} anonymous access codes generated. Export the roll to distribute them.')
        elif action == 'reset_all':
            req = integrity.request_approval(event, ApprovalRequest.Action.BULK_CREDENTIAL_RESET, {},
                                             request.POST.get('reason', ''), request.user, request)
            messages.success(request, 'Bulk reset executed.' if req.status == ApprovalRequest.Status.EXECUTED
                             else 'Bulk reset requested - a second official must approve it.')
        elif action == 'clear_unvoted':
            count = voter_service.clear_unvoted(event, request.user, request)
            messages.success(request, f'{count} voters removed.')
    except (voter_service.VoterImportError, lifecycle.LifecycleError, integrity.IntegrityControlError) as exc:
        messages.error(request, str(exc))
    return redirect('elections:console_voters', event_id=event.pk)


@event_permission_required('voter.credentials')
def voters_export(request, event):
    rows = voter_service.export_credentials(event, request.user, request)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="voter_roll_{event.pk}.csv"'
    writer = csv.writer(response)
    writer.writerow(['Voter ID', 'Name', 'Email', 'Constituency', 'Access code (unused only)', 'Status', 'Voted at'])
    for row in rows:
        writer.writerow([sanitize_csv_value(row[k]) for k in ('identifier', 'name', 'email', 'constituency', 'code',
                                                              'status', 'voted_at')])
    return response


@event_permission_required('election.view')
def eligibility(request, event):
    context = _base_context(request, event, 'eligibility')
    if request.method == 'POST':
        check_perm(request.user, 'election.edit', event)
        try:
            lifecycle.require_editable(event, 'config', request.user, request)
        except lifecycle.LifecycleError as exc:
            messages.error(request, str(exc))
            return redirect('elections:console_eligibility', event_id=event.pk)
        if request.POST.get('action') == 'delete':
            rule = get_object_or_404(EligibilityRule, pk=request.POST.get('rule'), election=event)
            audit.record('ELIGIBILITY_RULE_DELETED', request=request, event=event, target=rule, summary=str(rule))
            rule.delete()
            messages.success(request, 'Rule removed.')
        else:
            kind = request.POST.get('kind')
            if kind not in EligibilityRule.Kind.values:
                messages.error(request, 'Choose a rule type.')
                return redirect('elections:console_eligibility', event_id=event.pk)
            position = event.categories.filter(pk=request.POST.get('position') or 0).first()
            constituency = Constituency.objects.filter(organization_id=event.organization_id,
                                                       pk=request.POST.get('constituency') or 0).first()
            if kind == EligibilityRule.Kind.CONSTITUENCY and constituency is None:
                messages.error(request, 'Choose a constituency.')
                return redirect('elections:console_eligibility', event_id=event.pk)
            rule = EligibilityRule.objects.create(
                election=event, position=position, kind=kind, constituency=constituency,
                attribute=(request.POST.get('attribute') or '').strip()[:60],
                values=[v.strip() for v in (request.POST.get('values') or '').split(',') if v.strip()][:200],
                description=(request.POST.get('description') or '')[:255])
            audit.record('ELIGIBILITY_RULE_ADDED', request=request, event=event, target=rule, summary=str(rule),
                         metadata={'kind': kind, 'values': rule.values, 'attribute': rule.attribute})
            messages.success(request, 'Rule added.')
        return redirect('elections:console_eligibility', event_id=event.pk)
    context.update({'rules': event.eligibility_rules.select_related('position', 'constituency'),
                    'kinds': EligibilityRule.Kind.choices, 'positions': event.categories.all(),
                    'constituencies': Constituency.objects.filter(organization_id=event.organization_id) if event.organization_id else []})
    return render(request, 'console/elections/eligibility.html', context)


@login_required
def constituencies(request, org_id):
    organization = get_object_or_404(Organization, pk=org_id)
    check_perm(request.user, 'election.view', organization)
    if request.method == 'POST':
        check_perm(request.user, 'election.edit', organization)
        if request.POST.get('action') == 'delete':
            node = get_object_or_404(Constituency, pk=request.POST.get('node'), organization=organization)
            if node.voters.exists() or node.positions.exists():
                messages.error(request, 'This unit is in use by voters or positions.')
            else:
                audit.record('CONSTITUENCY_DELETED', request=request, organization=organization, target=node, summary=node.name)
                node.delete()
        else:
            name = (request.POST.get('name') or '').strip()[:150]
            code = (request.POST.get('code') or '').strip().upper()[:50]
            parent = Constituency.objects.filter(organization=organization, pk=request.POST.get('parent') or 0).first()
            kind = request.POST.get('kind') if request.POST.get('kind') in Constituency.Kind.values else Constituency.Kind.OTHER
            if not name or not code:
                messages.error(request, 'Name and code are required.')
            elif Constituency.objects.filter(organization=organization, code=code).exists():
                messages.error(request, 'That code already exists.')
            else:
                node = Constituency.objects.create(organization=organization, parent=parent, name=name, code=code, kind=kind)
                audit.record('CONSTITUENCY_CREATED', request=request, organization=organization, target=node,
                             summary=f'{node.get_kind_display()} {name} ({code})')
                messages.success(request, 'Unit added.')
        return redirect('elections:constituencies', org_id=organization.pk)
    nodes = Constituency.objects.filter(organization=organization).annotate(voter_count=Count('voters'))
    return render(request, 'console/constituencies.html', {'organization': organization, 'nodes': nodes,
                                                            'kinds': Constituency.Kind.choices})


@event_permission_required('election.view')
def results_view(request, event):
    context = _base_context(request, event, 'results')
    can_view = has_perm(request.user, 'results.view', event)
    context.update({
        'can_view_results': can_view,
        'official': event.results.filter(kind=ElectionResult.Kind.OFFICIAL).exclude(
            status=ElectionResult.Status.SUPERSEDED).first() if can_view else None,
        'certification': results_service.current_certification(event),
        'recounts': event.recounts.select_related('result', 'requested_by')[:20] if can_view else [],
        'shares': keys.shares_status(event) if event.key_custody == Event.KeyCustody.TRUSTEES else None,
        'my_share': TrusteeShare.objects.filter(election=event, trustee=request.user).first(),
    })
    official = context['official']
    if official is not None:
        context['separation_blocked'] = event.dual_approval_required and official.tallied_by_id == request.user.pk
    return render(request, 'console/elections/results.html', context)


@login_required
@require_POST
def results_action(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    action = request.POST.get('action')
    notes = (request.POST.get('notes') or '')[:2000]
    try:
        if action == 'tally':
            result = results_service.run_tally(event, actor=request.user, request=request)
            messages.success(request, f'Tally complete ({result.ballots_counted} ballots). It now needs review and certification.')
        elif action == 'certify':
            result = get_object_or_404(ElectionResult, pk=request.POST.get('result'), election=event)
            results_service.approve_and_certify(result, request.user, notes, request)
            messages.success(request, 'Results approved, signed and certified.')
        elif action == 'reject':
            result = get_object_or_404(ElectionResult, pk=request.POST.get('result'), election=event)
            results_service.reject_result(result, request.user, notes, request)
            messages.success(request, 'Result rejected. Run the tally again after resolving the issue.')
        elif action in ('recount', 'independent_recount'):
            kind = Recount.Kind.INDEPENDENT if action == 'independent_recount' else Recount.Kind.MANUAL
            record = results_service.recount(event, request.user, kind, notes, request)
            if record.matches:
                messages.success(request, 'Recount complete: it matches the official result exactly.')
            else:
                messages.warning(request, f'Recount complete: {len(record.differences)} difference(s) from the official result.')
        elif action == 'decertify':
            req = integrity.request_approval(event, ApprovalRequest.Action.DECERTIFY, {}, notes, request.user, request)
            messages.success(request, 'Certification revoked.' if req.status == ApprovalRequest.Status.EXECUTED
                             else 'Decertification requested - awaiting a second approver.')
        else:
            messages.error(request, 'Unknown action.')
    except PermissionDenied:
        messages.error(request, 'You do not have permission to do that.')
    except (results_service.ResultsError, keys.KeyCustodyError, lifecycle.LifecycleError,
            integrity.IntegrityControlError) as exc:
        messages.error(request, str(exc))
    return redirect('elections:console_results', event_id=event.pk)


@event_permission_required('election.view')
def results_export(request, event, fmt):
    from .exports import export

    certification = results_service.current_certification(event)
    if certification is not None:
        data = certification.result.data
    else:
        check_perm(request.user, 'results.view', event)
        result = event.results.filter(kind=ElectionResult.Kind.OFFICIAL).exclude(status=ElectionResult.Status.SUPERSEDED).first()
        if result is None:
            if event.is_paid and has_perm(request.user, 'vote.view', event):
                data = results_service.compute_paid_tally(event)
            else:
                raise Http404('No results to export yet.')
        else:
            data = result.data
    audit.record('RESULTS_EXPORTED', request=request, event=event, summary=f'Results exported as {fmt}')
    try:
        return export(event, data, fmt, certification)
    except ValueError:
        raise Http404 from None


@event_permission_required('election.view')
def trustees(request, event):
    context = _base_context(request, event, 'results')
    if request.method == 'POST':
        action = request.POST.get('action')
        try:
            if action == 'add':
                check_perm(request.user, 'election.edit', event)
                user = get_user_model().objects.filter(username=request.POST.get('username', '').strip()).first()
                if user is None:
                    messages.error(request, 'No such user.')
                else:
                    keys.add_trustee(event, user, request.user)
                    messages.success(request, f'{user.username} added as a trustee.')
            elif action == 'collect':
                share = keys.collect_share(event, request.user)
                return render(request, 'console/elections/trustee_share.html', {'event': event, 'share': share})
            elif action == 'submit':
                keys.submit_share(event, request.user, request.POST.get('share', ''))
                messages.success(request, 'Key share accepted for the tally.')
        except keys.KeyCustodyError as exc:
            messages.error(request, str(exc))
        return redirect('elections:console_trustees', event_id=event.pk)
    context.update({'trustee_rows': TrusteeShare.objects.filter(election=event).select_related('trustee'),
                    'status': keys.shares_status(event),
                    'my_share': TrusteeShare.objects.filter(election=event, trustee=request.user).first()})
    return render(request, 'console/elections/trustees.html', context)


@event_permission_required('election.view')
def integrity_view(request, event):
    context = _base_context(request, event, 'integrity')
    snapshots = list(event.config_snapshots.all()[:10])
    for snapshot in snapshots:
        snapshot.valid = integrity.verify_snapshot(snapshot)
    context.update({
        'snapshots': snapshots,
        'approvals': event.approval_requests.select_related('requested_by', 'decided_by')[:30],
        'history': AuditEvent.objects.filter(election_id=event.pk, event_type__startswith='ELECTION_')[:50],
        'disputes': event.disputes.all()[:50], 'incidents': event.incidents.all()[:50],
        'severities': Incident.Severity.choices, 'freeze_scopes': integrity.FREEZE_FLAGS,
    })
    return render(request, 'console/elections/integrity.html', context)


@login_required
@require_POST
def integrity_action(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    action = request.POST.get('action')
    reason = (request.POST.get('reason') or '').strip()
    new_end = ''
    if request.POST.get('new_end'):
        import zoneinfo

        from django.utils.dateparse import parse_datetime

        parsed = parse_datetime(request.POST['new_end'])
        if parsed is not None:
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, zoneinfo.ZoneInfo(event.timezone))
            new_end = parsed.isoformat()
    try:
        if action == 'freeze':
            integrity.freeze(event, request.POST.get('scope'), request.user, request)
            messages.success(request, 'Frozen.')
        elif action == 'unfreeze':
            req = integrity.unfreeze(event, request.POST.get('scope'), request.user, reason, request)
            messages.success(request, 'Unfrozen.' if req.status == ApprovalRequest.Status.EXECUTED
                             else 'Unfreeze requested - awaiting a second approver.')
        elif action == 'extend':
            req = integrity.request_approval(event, ApprovalRequest.Action.EXTEND_VOTING,
                                             {'new_end': new_end}, reason, request.user, request)
            messages.success(request, 'Voting extended.' if req.status == ApprovalRequest.Status.EXECUTED
                             else 'Extension requested - awaiting a second approver.')
        elif action == 'reopen':
            req = integrity.request_approval(event, ApprovalRequest.Action.REOPEN_VOTING,
                                             {'new_end': new_end}, reason, request.user, request)
            messages.success(request, 'Voting re-opened.' if req.status == ApprovalRequest.Status.EXECUTED
                             else 'Re-opening requested - awaiting a second approver.')
        elif action == 'legal_hold_on':
            integrity.set_legal_hold(event, request.user, True, reason, request)
            messages.success(request, 'Legal hold placed.')
        elif action == 'legal_hold_off':
            integrity.set_legal_hold(event, request.user, False, reason, request)
            messages.success(request, 'Release of legal hold requested.')
        elif action == 'snapshot':
            check_perm(request.user, 'election.view', event)
            integrity.snapshot_configuration(event, request.user, 'MANUAL')
            messages.success(request, 'Configuration snapshot signed.')
        elif action == 'incident':
            incident = integrity.open_incident(title=(request.POST.get('title') or '')[:200], actor=request.user,
                                               description=request.POST.get('description', ''), event=event,
                                               severity=request.POST.get('severity', 'MEDIUM'), request=request)
            return redirect('elections:console_incident', event_id=event.pk, incident_id=incident.pk)
        else:
            messages.error(request, 'Unknown action.')
    except PermissionDenied:
        messages.error(request, 'You do not have permission to do that.')
    except (integrity.IntegrityControlError, lifecycle.LifecycleError, KeyError) as exc:
        messages.error(request, str(exc) or 'Invalid request.')
    return redirect('elections:console_integrity', event_id=event.pk)


def _case_post(request, event, dispute=None, incident=None):
    action = request.POST.get('action')
    if action == 'note' and request.POST.get('body'):
        integrity.add_note(request.user, request.POST['body'][:5000], dispute=dispute, incident=incident, request=request)
    elif action == 'status':
        if dispute is not None:
            integrity.update_dispute(dispute, request.user, request.POST.get('status'), request.POST.get('resolution', ''), request)
        else:
            integrity.update_incident(incident, request.user, request.POST.get('status'), request.POST.get('note', ''), request)
    elif action == 'evidence' and request.FILES.get('file'):
        from core.storage import validate_document

        uploaded = request.FILES['file']
        error = validate_document(uploaded, max_bytes=25 * 1024 * 1024, allowed={
            '.pdf': b'%PDF', '.png': b'\x89PNG', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff'})
        if error:
            messages.error(request, error)
        else:
            integrity.preserve_evidence(event, uploaded, (request.POST.get('title') or uploaded.name)[:200], request.user,
                                        request.POST.get('description', ''), dispute=dispute, incident=incident, request=request)
            messages.success(request, 'Evidence preserved (SHA-256 recorded).')


@event_permission_required('dispute.view')
def dispute_detail(request, event, dispute_id):
    dispute = get_object_or_404(Dispute, pk=dispute_id, election=event)
    if request.method == 'POST':
        check_perm(request.user, 'dispute.manage', event)
        _case_post(request, event, dispute=dispute)
        return redirect('elections:console_dispute', event_id=event.pk, dispute_id=dispute.pk)
    context = _base_context(request, event, 'integrity')
    context.update({'dispute': dispute, 'statuses': Dispute.Status.choices, 'notes': dispute.notes.select_related('author'),
                    'evidence': dispute.evidence.select_related('uploaded_by'),
                    'can_manage': has_perm(request.user, 'dispute.manage', event)})
    return render(request, 'console/elections/dispute.html', context)


@event_permission_required('election.view')
def incident_detail(request, event, incident_id):
    incident = get_object_or_404(Incident, pk=incident_id, election=event)
    if request.method == 'POST':
        check_perm(request.user, 'incident.manage', event)
        _case_post(request, event, incident=incident)
        return redirect('elections:console_incident', event_id=event.pk, incident_id=incident.pk)
    context = _base_context(request, event, 'integrity')
    context.update({'incident': incident, 'statuses': Incident.Status.choices, 'notes': incident.notes.select_related('author'),
                    'evidence': incident.evidence.select_related('uploaded_by'),
                    'can_manage': has_perm(request.user, 'incident.manage', event)})
    return render(request, 'console/elections/incident.html', context)


@event_permission_required('election.view')
def evidence_download(request, event, item_id):
    item = get_object_or_404(EvidenceItem, pk=item_id, election=event)
    if not (has_perm(request.user, 'dispute.view', event) or has_perm(request.user, 'incident.manage', event)):
        raise PermissionDenied
    audit.record('EVIDENCE_ACCESSED', request=request, event=event, target=item, summary=f'Evidence "{item.title}" downloaded')
    response = FileResponse(item.file.open('rb'), as_attachment=True, filename=item.file.name.rsplit('/', 1)[-1])
    response['X-Evidence-SHA256'] = item.sha256
    return response


@event_permission_required('audit.view')
def audit_log(request, event):
    context = _base_context(request, event, 'audit')
    entries = AuditEvent.objects.filter(election_id=event.pk)
    event_type = (request.GET.get('type') or '').strip().upper()
    if event_type:
        entries = entries.filter(event_type__startswith=event_type)
    context.update({'page': Paginator(entries, 50).get_page(request.GET.get('page')), 'type': event_type})
    if request.GET.get('verify'):
        from core.audit import chain_for, verify_chain

        context['verification'] = verify_chain(chain_for(event.organization_id))
        audit.record('AUDIT_CHAIN_VERIFIED', request=request, event=event, summary='Audit chain verified on demand',
                     metadata={'ok': context['verification'][0]})
    return render(request, 'console/elections/audit.html', context)


@event_permission_required('vote.view')
def monitor(request, event):
    context = _base_context(request, event, 'monitor')
    return render(request, 'console/elections/monitor.html', context)


@event_permission_required('vote.view')
def monitor_data(request, event):
    from fraud.models import FraudEvent
    from payments.models import Payment

    now = timezone.now()
    since = now - timedelta(minutes=60)
    if event.is_paid:
        series = VoteTransaction.objects.filter(candidate__event=event, status='Success', created_at__gte=since) \
            .annotate(minute=TruncMinute('created_at')).values('minute').annotate(n=Sum('number_of_votes')).order_by('minute')
        payments = Payment.objects.filter(event=event, created_at__gte=since)
        attempted = payments.count()
        succeeded = payments.filter(status='SUCCESS').count()
        extra = {'payments_last_hour': attempted, 'payment_success_rate': round(100.0 * succeeded / attempted, 1) if attempted else None,
                 'held': Payment.objects.filter(event=event, held=True, status='SUCCESS').count()}
    else:
        series = event.voters.filter(voted_at__gte=since).annotate(minute=TruncMinute('voted_at')).values('minute') \
            .annotate(n=Count('pk')).order_by('minute')
        extra = {'turnout': voter_service.turnout(event)}
    alerts = FraudEvent.objects.filter(event=event, status=FraudEvent.Status.OPEN)
    return JsonResponse({
        'status': event.status, 'generated_at': now.isoformat(),
        'series': [{'t': row['minute'].isoformat(), 'n': row['n']} for row in series],
        'last_5_min': sum(row['n'] for row in series if row['minute'] >= now - timedelta(minutes=5)),
        'open_alerts': alerts.count(),
        'latest_alerts': [{'id': a.pk, 'kind': a.kind, 'score': a.score, 'decision': a.decision,
                           'at': a.created_at.isoformat()} for a in alerts[:5]],
        **extra,
    })
