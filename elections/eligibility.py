"""Voter eligibility and ballot styles (which positions a voter may vote on)."""
from .models import EligibilityRule, Voter

R = EligibilityRule.Kind


def _attribute(voter, name):
    value = (voter.attributes or {}).get(name)
    return '' if value is None else str(value).strip().lower()


def rule_passes(rule, voter):
    values = [str(v).strip().lower() for v in (rule.values or [])]
    if rule.kind == R.CONSTITUENCY:
        return bool(voter.constituency_id and rule.constituency_id and voter.constituency.is_within(rule.constituency))
    if rule.kind == R.ATTRIBUTE_EQUALS:
        return _attribute(voter, rule.attribute) == (values[0] if values else '')
    if rule.kind == R.ATTRIBUTE_IN:
        return _attribute(voter, rule.attribute) in values
    if rule.kind == R.ATTRIBUTE_NOT_IN:
        return _attribute(voter, rule.attribute) not in values
    if rule.kind == R.EMAIL_DOMAIN:
        email = (voter.email or '').lower()
        return bool(email) and email.rsplit('@', 1)[-1] in [v.lstrip('@') for v in values]
    if rule.kind == R.VERIFIED_EMAIL:
        return voter.email_verified_at is not None
    if rule.kind == R.VERIFIED_PHONE:
        return voter.phone_verified_at is not None
    return False


def _rules(event):
    cached = getattr(event, '_fv_rules', None)
    if cached is None:
        cached = list(event.eligibility_rules.filter(is_active=True).select_related('constituency', 'position'))
        event._fv_rules = cached
    return cached


def election_eligibility(event, voter):
    """(eligible, [reasons]) for the election as a whole."""
    reasons = []
    if voter.status == Voter.Status.SUSPENDED:
        reasons.append('Your voter record is suspended. Contact the election officials.')
    elif voter.status == Voter.Status.INELIGIBLE:
        reasons.append('You are not eligible to vote in this election.')
    for rule in _rules(event):
        if rule.position_id is None and not rule_passes(rule, voter):
            reasons.append(rule.description or f'Eligibility requirement not met: {rule.get_kind_display()}.')
    return not reasons, reasons


def position_eligible(position, voter, rules):
    if position.constituency_id:
        if not voter.constituency_id or not voter.constituency.is_within(position.constituency):
            return False
    return all(rule_passes(rule, voter) for rule in rules if rule.position_id == position.pk)


def ballot_style(event, voter):
    """Sorted list of position ids this voter may vote on."""
    eligible, _ = election_eligibility(event, voter)
    if not eligible:
        return []
    rules = _rules(event)
    return sorted(p.pk for p in event.categories.select_related('constituency') if position_eligible(p, voter, rules))


def eligible_voter_count(event):
    return event.voters.exclude(status__in=[Voter.Status.SUSPENDED, Voter.Status.INELIGIBLE]).count()
