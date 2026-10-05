"""Ballot structure and validation for every supported ballot type.

A submitted ballot is ``{position_id: selection}`` where the selection shape
depends on the position's ballot type:

=============  ===============================================
SINGLE         ``[candidate_id]``
FPTP           ``[candidate_id, ...]`` (up to max_select / seats)
MULTIPLE       ``[candidate_id, ...]`` within min/max
APPROVAL       ``[candidate_id, ...]`` any number of approvals
RANKED         ``[first_choice_id, second_choice_id, ...]``
SCORE          ``{candidate_id: score}`` with 0 <= score <= max_score
REFERENDUM     ``"YES"`` or ``"NO"``
=============  ===============================================

An empty selection is an abstention (only if the position allows it).
Anything malformed rejects the *whole* ballot - a partial ballot is never
recorded.
"""
from voting.models import Candidate, Category, Event

BT = Category.BallotType
REFERENDUM_OPTIONS = ('YES', 'NO')


class BallotError(Exception):
    def __init__(self, message, position=None):
        super().__init__(message)
        self.message = message
        self.position = position


def positions_for(event):
    return list(event.categories.all().prefetch_related('candidates'))


def active_candidates(position):
    return [c for c in position.candidates.all() if c.status == Candidate.Status.ACTIVE]


def effective_limits(position):
    """(min_select, max_select) after applying ballot-type semantics."""
    candidates = len(active_candidates(position))
    if position.ballot_type == BT.SINGLE:
        return min(position.min_select, 1), 1
    if position.ballot_type == BT.APPROVAL:
        return min(position.min_select, max(candidates, 1)), max(candidates, 1)
    if position.ballot_type == BT.REFERENDUM:
        return 1, 1
    if position.ballot_type == BT.SCORE:
        return position.min_select, max(candidates, 1)
    if position.ballot_type == BT.FPTP:
        maximum = max(position.max_select, position.seats)
        return min(position.min_select, maximum), maximum
    return position.min_select, max(position.max_select, position.min_select)


def ballot_definition(event, style=None, include_bio=True):
    """Serializable ballot for rendering (web) and the API."""
    style = set(style) if style is not None else None
    definition = []
    for position in positions_for(event):
        if style is not None and position.pk not in style:
            continue
        minimum, maximum = effective_limits(position)
        definition.append({
            'id': position.pk,
            'name': position.name,
            'description': position.description,
            'ballot_type': position.ballot_type,
            'ballot_type_label': position.get_ballot_type_display(),
            'min_select': minimum,
            'max_select': maximum,
            'seats': position.seats,
            'max_score': position.max_score,
            'allow_abstain': position.allow_abstain,
            'referendum_threshold': float(position.referendum_threshold),
            'options': list(REFERENDUM_OPTIONS) if position.is_referendum else [],
            'candidates': [] if position.is_referendum else [
                {
                    'id': c.pk, 'name': c.name, 'affiliation': c.affiliation,
                    'bio': c.bio if include_bio else '', 'manifesto': c.manifesto if include_bio else '',
                    'image_url': c.image.url if c.image else '',
                }
                for c in active_candidates(position)
            ],
        })
    return definition


def rules_text(position_def):
    t = position_def['ballot_type']
    lo, hi = position_def['min_select'], position_def['max_select']
    if t == BT.REFERENDUM:
        text = 'Vote YES or NO.'
    elif t == BT.SINGLE:
        text = 'Choose one candidate.'
    elif t == BT.RANKED:
        text = f'Rank up to {hi} candidate(s) in order of preference (1 = first choice).'
    elif t == BT.SCORE:
        text = f'Give each candidate a score from 0 to {position_def["max_score"]}.'
    elif t == BT.APPROVAL:
        text = 'Approve as many candidates as you like.'
    elif lo == hi:
        text = f'Choose exactly {hi} candidate(s).'
    else:
        text = f'Choose between {lo} and {hi} candidate(s).'
    if position_def['allow_abstain']:
        text += ' You may abstain.'
    return text


def _as_list(raw):
    if raw is None or raw == '':
        return []
    if isinstance(raw, (list, tuple)):
        return [r for r in raw if r not in (None, '')]
    return [raw]


def _to_ids(values, valid_ids, position):
    ids = []
    for value in values:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            raise BallotError(f'Invalid selection for "{position.name}".', position) from None
    if len(set(ids)) != len(ids):
        raise BallotError(f'The same candidate was selected more than once for "{position.name}".', position)
    if not set(ids) <= valid_ids:
        raise BallotError(f'Invalid candidate selected for "{position.name}".', position)
    return ids


def normalize_selection(position, raw):
    """Validate one position's raw selection and return its canonical form
    (``None`` means abstained)."""
    valid_ids = {c.pk for c in active_candidates(position)}
    minimum, maximum = effective_limits(position)
    bt = position.ballot_type

    if bt == BT.REFERENDUM:
        values = _as_list(raw)
        if not values:
            if not position.allow_abstain:
                raise BallotError(f'Please vote YES or NO on "{position.name}".', position)
            return None
        if len(values) != 1 or str(values[0]).upper() not in REFERENDUM_OPTIONS:
            raise BallotError(f'Invalid answer for "{position.name}".', position)
        return str(values[0]).upper()

    if bt == BT.SCORE:
        if raw in (None, '', {}):
            if not position.allow_abstain:
                raise BallotError(f'Please score the candidates for "{position.name}".', position)
            return None
        if not isinstance(raw, dict):
            raise BallotError(f'Invalid scores for "{position.name}".', position)
        scores = {}
        for key, value in raw.items():
            if value in (None, ''):
                continue
            try:
                cid, score = int(key), int(value)
            except (TypeError, ValueError):
                raise BallotError(f'Invalid score for "{position.name}".', position) from None
            if cid not in valid_ids:
                raise BallotError(f'Invalid candidate scored for "{position.name}".', position)
            if not 0 <= score <= position.max_score:
                raise BallotError(f'Scores for "{position.name}" must be between 0 and {position.max_score}.', position)
            scores[str(cid)] = score
        if not scores:
            if not position.allow_abstain:
                raise BallotError(f'Please score the candidates for "{position.name}".', position)
            return None
        if len(scores) < minimum:
            raise BallotError(f'Score at least {minimum} candidate(s) for "{position.name}".', position)
        return dict(sorted(scores.items(), key=lambda kv: int(kv[0])))

    values = _as_list(raw)
    if not values:
        if not position.allow_abstain:
            raise BallotError(f'You must make a selection for "{position.name}".', position)
        return None
    ids = _to_ids(values, valid_ids, position)
    if len(ids) > maximum:
        raise BallotError(f'"{position.name}" allows at most {maximum} selection(s).', position)
    if len(ids) < minimum:
        raise BallotError(f'"{position.name}" requires at least {minimum} selection(s).', position)
    if bt == BT.RANKED:
        return ids  # order is meaningful
    return sorted(ids)


def validate_ballot(event, style, payload):
    """Validate a whole ballot. ``style`` is the list of position ids this
    voter may vote on; selections for other positions are rejected."""
    if not isinstance(payload, dict):
        raise BallotError('Malformed ballot.')
    allowed = set(style)
    unknown = {str(k) for k in payload} - {str(p) for p in allowed}
    if unknown:
        raise BallotError('The ballot contains positions you are not eligible to vote on.')
    selections = {}
    for position in positions_for(event):
        if position.pk not in allowed:
            continue
        raw = payload.get(str(position.pk), payload.get(position.pk))
        selections[str(position.pk)] = normalize_selection(position, raw)
    return selections


def describe_selections(event, selections):
    """Human-readable review list: [(position name, [choice strings]), ...]."""
    names = {c.pk: c.name for c in Candidate.objects.filter(event=event)}
    rows = []
    for position in positions_for(event):
        key = str(position.pk)
        if key not in selections:
            continue
        value = selections[key]
        if value is None:
            choices = ['Abstained']
        elif position.ballot_type == BT.REFERENDUM:
            choices = [value.title()]
        elif position.ballot_type == BT.SCORE:
            choices = [f'{names.get(int(cid), "?")}: {score}' for cid, score in value.items()]
        elif position.ballot_type == BT.RANKED:
            choices = [f'{rank}. {names.get(cid, "?")}' for rank, cid in enumerate(value, start=1)]
        else:
            choices = [names.get(cid, '?') for cid in value]
        rows.append({'position': position.name, 'position_id': position.pk, 'choices': choices,
                     'abstained': value is None})
    return rows


def configuration_problems(event):
    """Everything that must be fixed before an election can be submitted/scheduled."""
    problems = []
    if event.end_date <= event.start_date:
        problems.append('The end date must be after the start date.')
    positions = positions_for(event)
    if not positions and not event.candidates.exists():
        problems.append('Add at least one position or contestant.')
    if event.is_institutional:
        if not positions:
            problems.append('Institutional elections need at least one position.')
        if event.candidates.filter(category__isnull=True).exists():
            problems.append('Assign every candidate to a position.')
        for position in positions:
            if position.is_referendum:
                continue
            count = len(active_candidates(position))
            minimum, maximum = effective_limits(position)
            if count == 0:
                problems.append(f'"{position.name}" has no candidates.')
            elif position.ballot_type in (BT.MULTIPLE, BT.RANKED, BT.FPTP) and minimum > count:
                problems.append(f'"{position.name}" requires {minimum} selections but has only {count} candidate(s).')
            if position.seats > max(count, 1):
                problems.append(f'"{position.name}" has more seats than candidates.')
            if position.min_select > position.max_select and position.ballot_type in (BT.MULTIPLE, BT.RANKED, BT.FPTP):
                problems.append(f'"{position.name}": minimum selections exceed the maximum.')
        if not event.auth_methods:
            problems.append('Choose at least one voter authentication method.')
        if not event.voters.exists() and not event.allow_self_registration:
            problems.append('Import the voter roll (or enable self-registration).')
        if event.key_custody == Event.KeyCustody.TRUSTEES:
            trustees = event.trustee_shares.count()
            if trustees < 2 or not 1 <= event.trustee_threshold <= trustees:
                problems.append('Trustee key custody needs at least 2 trustees and a threshold between 1 and the number of trustees.')
    else:
        if event.vote_price <= 0 and not event.vote_packages.filter(is_active=True).exists():
            problems.append('Set a vote price or create at least one vote package.')
    return problems
