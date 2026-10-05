"""Public pages: candidates, results, verification, disputes."""
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from core import captcha, ratelimit
from core.utils import client_ip
from voting.models import Candidate, Event

from . import integrity, lifecycle, results as results_service
from .models import Dispute


def _public_event(event_id):
    event = get_object_or_404(Event.objects.select_related('organization'), pk=event_id)
    if not event.is_public and event.status != Event.Status.ARCHIVED:
        raise Http404
    return lifecycle.tick(event)


def candidates(request, event_id):
    event = _public_event(event_id)
    positions = event.categories.prefetch_related('candidates')
    return render(request, 'public/candidates.html', {'event': event, 'positions': positions,
                                                      'uncategorized': event.candidates.filter(category__isnull=True)})


def candidate_profile(request, event_id, candidate_id):
    event = _public_event(event_id)
    candidate = get_object_or_404(Candidate.objects.select_related('category'), pk=candidate_id, event=event)
    return render(request, 'public/candidate.html', {'event': event, 'candidate': candidate,
                                                     'documents': candidate.documents.all()})


def results(request, event_id):
    event = _public_event(event_id)
    public = results_service.public_results(event)
    fmt = request.GET.get('format')
    if fmt and public and public['certified']:
        from .exports import export

        return export(event, public['data'], fmt, public['certification'])
    total_votes = sum(p.get('total_votes') or 0 for p in public['data']['positions']) if public else 0
    return render(request, 'public/results.html', {'event': event, 'public': public, 'total_votes': total_votes})


def results_index(request):
    events = Event.objects.filter(status__in=[Event.Status.PUBLISHED, Event.Status.ARCHIVED]).select_related(
        'organization').order_by('-published_at', '-end_date')
    query = (request.GET.get('q') or '').strip()[:100]
    if query:
        events = events.filter(title__icontains=query)
    return render(request, 'public/results_index.html', {'page': Paginator(events, 20).get_page(request.GET.get('page')),
                                                         'query': query})


def results_api(request, event_id):
    """Machine-readable results (also available under /api/v1/)."""
    event = _public_event(event_id)
    public = results_service.public_results(event)
    if public is None:
        return JsonResponse({'error': {'code': 'not_available', 'message': 'Results are not public yet.'}}, status=404)
    payload = {'certified': public['certified'], 'results': public['data']}
    if public['certification']:
        payload['certification'] = {'signature': public['certification'].signature,
                                    'public_key': public['certification'].public_key,
                                    'payload': public['certification'].payload}
    return JsonResponse(payload)


def verify(request, event_id):
    event = _public_event(event_id)
    bundle = results_service.verification_bundle(event)
    checks = results_service.verify_bundle(bundle)
    tracker = (request.GET.get('tracker') or '').strip().lower()
    proof = None
    if tracker:
        allowed, retry = ratelimit.hit('tracker-lookup', client_ip(request) or 'unknown', 30, 60)
        if not allowed:
            return ratelimit.too_many_requests(request, retry)
        if len(tracker) == 64 and all(c in '0123456789abcdef' for c in tracker):
            proof = results_service.tracker_proof(event, tracker)
        else:
            proof = {'included': False, 'invalid': True}
    turnout = None
    if event.is_institutional:
        from .voters import turnout as turnout_stats

        turnout = turnout_stats(event) if event.status in lifecycle.LOCKED_STATES else None
    return render(request, 'public/verify.html', {
        'event': event, 'bundle': bundle, 'checks': checks, 'all_ok': all(ok for _, ok, _ in checks) if checks else None,
        'tracker': tracker, 'proof': proof, 'turnout': turnout,
        'certification': results_service.current_certification(event),
    })


def verify_bundle(request, event_id):
    event = _public_event(event_id)
    response = JsonResponse(results_service.verification_bundle(event), json_dumps_params={'indent': 2})
    response['Content-Disposition'] = f'attachment; filename="verification_{event.pk}.json"'
    return response


def file_dispute(request, event_id):
    event = _public_event(event_id)
    if request.method == 'POST':
        allowed, retry = ratelimit.hit('dispute', client_ip(request) or 'unknown', 3, 3600)
        if not allowed:
            return ratelimit.too_many_requests(request, retry)
        human, _ = captcha.verify_human(request)
        name = (request.POST.get('name') or '').strip()[:150]
        email = (request.POST.get('email') or '').strip()
        subject = (request.POST.get('subject') or '').strip()[:200]
        description = (request.POST.get('description') or '').strip()[:10000]
        try:
            validate_email(email)
        except ValidationError:
            human = False
        if not (human and name and subject and description):
            messages.error(request, 'Please fill in every field with valid details.')
            return render(request, 'public/dispute.html', {'event': event, 'form': request.POST,
                                                           'categories': Dispute.Category.choices}, status=400)
        dispute = integrity.file_dispute(
            event, filer_name=name, filer_email=email, filer_role=(request.POST.get('role') or 'VOTER')[:40],
            category=request.POST.get('category') if request.POST.get('category') in Dispute.Category.values else Dispute.Category.OTHER,
            subject=subject, description=description, user=request.user, request=request)
        messages.success(request, f'Your dispute has been filed (reference {dispute.reference}). '
                                  'Election officials will review it.')
        return redirect('event_detail', event_id=event.pk)
    return render(request, 'public/dispute.html', {'event': event, 'categories': Dispute.Category.choices})
