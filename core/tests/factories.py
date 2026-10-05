"""Test data builders shared by every app's tests."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone

from core.models import Organization, Role, RoleAssignment
from core.rbac import ROLE_DEFINITIONS, clear_cache
from voting.models import Candidate, Category, Event

PASSWORD = 'a-Very-strong-pass-2026'


def ensure_roles():
    for code, (name, description, permissions) in ROLE_DEFINITIONS.items():
        Role.objects.update_or_create(code=code, defaults={'name': name, 'description': description,
                                                           'permissions': sorted(permissions)})


def make_user(username='user', email=None, staff=False, superuser=False, password=PASSWORD):
    User = get_user_model()
    user = User.objects.create_user(username=username, email=email or f'{username}@example.com', password=password,
                                    is_staff=staff, is_superuser=superuser)
    return user


def make_org(name='Test University', admin=None, kind=Organization.Kind.UNIVERSITY):
    ensure_roles()
    org = Organization.objects.create(name=name, kind=kind)
    if admin is not None:
        grant(admin, 'ORG_ADMIN', org)
    return org


def grant(user, role_code, org=None, event=None):
    ensure_roles()
    assignment, _ = RoleAssignment.objects.get_or_create(user=user, role=Role.objects.get(code=role_code),
                                                         organization=org, event=event)
    clear_cache(user)
    return assignment


def make_event(org=None, organizer=None, institutional=True, status=Event.Status.DRAFT, start_offset=timedelta(hours=-1),
               end_offset=timedelta(days=1), **kwargs):
    now = timezone.now()
    defaults = {
        'title': 'Student Union Election 2026' if institutional else 'Star Search Season 5',
        'organization': org, 'organizer': organizer, 'status': status,
        'voting_mode': Event.VotingMode.CODE_VOTING if institutional else Event.VotingMode.PAY_TO_VOTE,
        'start_date': now + start_offset, 'end_date': now + end_offset,
        'vote_price': Decimal('0') if institutional else Decimal('1.00'),
        'results_visibility': Event.ResultsVisibility.AFTER_PUBLISH if institutional else Event.ResultsVisibility.LIVE,
        'auth_methods': ['CODE'],
    }
    defaults.update(kwargs)
    return Event.objects.create(**defaults)


def add_position(event, name='President', ballot_type=Category.BallotType.SINGLE, candidates=('Alice', 'Bob', 'Chidi'),
                 **kwargs):
    position = Category.objects.create(event=event, name=name, ballot_type=ballot_type, **kwargs)
    made = [Candidate.objects.create(event=event, category=position, name=n) for n in candidates]
    return position, made


def add_voters(event, count=3, prefix='ST', with_email=True):
    from elections.models import Voter

    voters, codes = [], {}
    for i in range(1, count + 1):
        voter = Voter(election=event, identifier=f'{prefix}{i:03d}', full_name=f'Voter {i}')
        if with_email:
            voter.set_email(f'{prefix.lower()}{i}@uni.edu')
        code = voter.issue_credential()
        voter.save()
        voters.append(voter)
        codes[voter.pk] = code
    return voters, codes


def open_institutional(event, admin, reviewer=None):
    """Drive an election through review/approval/scheduling to OPEN."""
    from elections import lifecycle

    lifecycle.transition(event, 'submit', actor=admin)
    lifecycle.transition(event, 'approve', actor=reviewer or admin)
    lifecycle.transition(event, 'schedule', actor=admin)
    event.refresh_from_db()
    if event.status == Event.Status.SCHEDULED:
        lifecycle.transition(event, 'open', actor=admin)
    event.refresh_from_db()
    return event


def institutional_setup(dual=False, prefix=''):
    """An org with an admin and a reviewer, an election with two positions,
    three voters and their codes."""
    admin = make_user(f'{prefix}admin1')
    reviewer = make_user(f'{prefix}reviewer1')
    org = make_org(name=f'{prefix}Test University', admin=admin)
    grant(reviewer, 'ELECTION_REVIEWER', org)
    grant(reviewer, 'RESULTS_OFFICER', org)
    event = make_event(org=org, organizer=admin, dual_approval_required=dual)
    president, president_candidates = add_position(event)
    senate, senate_candidates = add_position(event, 'Senate', Category.BallotType.MULTIPLE, ('Dede', 'Efua', 'Fiifi'),
                                             min_select=1, max_select=2, seats=2)
    voters, codes = add_voters(event, prefix=f'{prefix.upper()}ST')
    return {'admin': admin, 'reviewer': reviewer, 'org': org, 'event': event, 'president': president,
            'president_candidates': president_candidates, 'senate': senate, 'senate_candidates': senate_candidates,
            'voters': voters, 'codes': codes}
