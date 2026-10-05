"""Candidate portal.

Candidates manage their own profile and see public information only:
aggregate statistics and approved/published results - never anything about
individual voters or ballots.
"""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core import audit, auth as staff_auth
from core.rbac import event_permission_required
from core.storage import file_sha256, validate_document
from voting.models import Candidate, VoteTransaction

from . import lifecycle, results as results_service
from .models import CandidateDocument

INVITE_SALT = 'flexyvotes.candidate-invite'
INVITE_MAX_AGE = 7 * 24 * 3600


def invitation_link(candidate):
    token = signing.dumps({'candidate': candidate.pk, 'email': candidate.email.lower()}, salt=INVITE_SALT)
    return f'{settings.SITE_URL}/portal/accept/{token}/'


@event_permission_required('candidate.edit')
@require_POST
def invite_candidate(request, event, candidate_id):
    from notifications.models import Notification
    from notifications.service import notify

    candidate = get_object_or_404(Candidate, pk=candidate_id, event=event)
    if not candidate.email:
        messages.error(request, 'Add an email address to the candidate first.')
    else:
        notify('candidate_invitation', channel=Notification.Channel.EMAIL, recipient=candidate.email, event=event,
               context={'name': candidate.name, 'link': invitation_link(candidate)})
        audit.record('CANDIDATE_INVITED', request=request, event=event, target=candidate,
                     summary=f'Portal invitation sent to {candidate.name}')
        messages.success(request, f'Invitation sent to {candidate.name}.')
    return redirect('elections:console_ballot', event_id=event.pk)


def accept_invitation(request, token):
    try:
        data = signing.loads(token, salt=INVITE_SALT, max_age=INVITE_MAX_AGE)
    except signing.BadSignature:
        messages.error(request, 'This invitation link is invalid or has expired.')
        return redirect('home')
    candidate = get_object_or_404(Candidate, pk=data['candidate'])
    if candidate.email.lower() != data['email']:
        messages.error(request, 'This invitation is no longer valid.')
        return redirect('home')
    if request.user.is_authenticated:
        if candidate.user_id and candidate.user_id != request.user.pk:
            messages.error(request, 'This candidate profile is already linked to another account.')
            return redirect('home')
        candidate.user = request.user
        candidate.save(update_fields=['user'])
        audit.record('CANDIDATE_PORTAL_LINKED', request=request, event=candidate.event, target=candidate,
                     summary=f'{request.user.username} linked to candidate {candidate.name}')
        return redirect('portal:candidate', candidate_id=candidate.pk)
    if request.method == 'POST':
        User = get_user_model()
        username = (request.POST.get('username') or '').strip()[:150]
        password = request.POST.get('password') or ''
        errors = []
        if not username or User.objects.filter(username__iexact=username).exists():
            errors.append('Choose a different username.')
        try:
            validate_password(password, user=User(username=username, email=candidate.email))
        except ValidationError as exc:
            errors.extend(exc.messages)
        if errors:
            for error in errors:
                messages.error(request, error)
            return render(request, 'portal/accept.html', {'candidate': candidate})
        user = User.objects.create_user(username=username, email=candidate.email, password=password)
        candidate.user = user
        candidate.save(update_fields=['user'])
        staff_auth.complete_login(request, user, 'invitation')
        audit.record('CANDIDATE_PORTAL_LINKED', request=request, event=candidate.event, target=candidate,
                     summary=f'Candidate account {username} created from invitation')
        return redirect('portal:candidate', candidate_id=candidate.pk)
    return render(request, 'portal/accept.html', {'candidate': candidate})


@login_required
def portal_home(request):
    candidacies = Candidate.objects.filter(user=request.user).select_related('event', 'category')
    return render(request, 'portal/home.html', {'candidacies': candidacies})


def _own_candidate(request, candidate_id):
    candidate = get_object_or_404(Candidate.objects.select_related('event', 'category', 'category__constituency'),
                                  pk=candidate_id)
    if candidate.user_id != request.user.pk:
        raise PermissionDenied
    return candidate


def _campaign_stats(candidate):
    event = candidate.event
    stats = {'position': candidate.category.name if candidate.category_id else 'All contestants'}
    if event.is_paid and event.results_are_public:
        ledger = VoteTransaction.objects.filter(candidate=candidate, status='Success')
        totals = ledger.aggregate(votes=Sum('number_of_votes'), supporters=Count('pk'))
        stats.update({'votes': totals['votes'] or 0, 'transactions': totals['supporters'] or 0})
        live = results_service.live_results(event)
        for position in live['positions']:
            for row in position['candidates']:
                if row['candidate_id'] == candidate.pk:
                    stats.update({'rank': row['rank'], 'percentage': row['percentage'], 'of': len(position['candidates'])})
    if event.is_institutional and event.status in lifecycle.LOCKED_STATES:
        from .voters import turnout

        data = turnout(event)
        stats.update({'turnout': data['turnout_percentage'], 'votes_cast': data['voted']})
    return stats


@login_required
def portal_candidate(request, candidate_id):
    candidate = _own_candidate(request, candidate_id)
    event = candidate.event
    allowed, _, reason = lifecycle.edit_policy(event, 'candidate_profile')
    if request.method == 'POST':
        if not allowed:
            messages.error(request, reason)
            return redirect('portal:candidate', candidate_id=candidate.pk)
        action = request.POST.get('action', 'profile')
        if action == 'profile':
            fields = ['bio', 'manifesto', 'affiliation', 'image']
            before = audit.snapshot(candidate, fields)
            candidate.bio = (request.POST.get('bio') or '')[:5000]
            candidate.manifesto = (request.POST.get('manifesto') or '')[:20000]
            candidate.affiliation = (request.POST.get('affiliation') or '')[:120]
            photo = request.FILES.get('image')
            if photo is not None:
                error = validate_document(photo, max_bytes=2 * 1024 * 1024,
                                          allowed={'.png': b'\x89PNG', '.jpg': b'\xff\xd8\xff', '.jpeg': b'\xff\xd8\xff'})
                if error:
                    messages.error(request, error)
                    return redirect('portal:candidate', candidate_id=candidate.pk)
                candidate.image = photo
            candidate.save()
            audit.record('CANDIDATE_PROFILE_UPDATED', request=request, event=event, target=candidate,
                         summary='Candidate updated their profile', changes=audit.diff(before, audit.snapshot(candidate, fields)))
            messages.success(request, 'Profile saved.')
        elif action == 'document':
            uploaded = request.FILES.get('document')
            error = validate_document(uploaded, max_bytes=10 * 1024 * 1024, allowed={'.pdf': b'%PDF'})
            if error:
                messages.error(request, error)
            else:
                document = CandidateDocument.objects.create(candidate=candidate, file=uploaded, uploaded_by=request.user,
                                                            title=(request.POST.get('title') or uploaded.name)[:150],
                                                            sha256=file_sha256(uploaded))
                audit.record('CANDIDATE_DOCUMENT_UPLOADED', request=request, event=event, target=document,
                             summary=f'Document "{document.title}" uploaded', metadata={'sha256': document.sha256})
                messages.success(request, 'Document uploaded.')
        elif action == 'delete_document':
            document = get_object_or_404(CandidateDocument, pk=request.POST.get('document'), candidate=candidate)
            audit.record('CANDIDATE_DOCUMENT_DELETED', request=request, event=event, target=document,
                         summary=f'Document "{document.title}" removed')
            document.delete()
        return redirect('portal:candidate', candidate_id=candidate.pk)
    return render(request, 'portal/candidate.html', {
        'candidate': candidate, 'event': event, 'editable': allowed, 'edit_reason': reason,
        'documents': candidate.documents.all(), 'stats': _campaign_stats(candidate),
        'public_results': results_service.public_results(event),
    })
