"""Pure, deterministic tally algorithms (no database access).

Every function takes the list of normalized selections cast for one position
(``None`` = abstention) and returns a JSON-serializable result. Ties that
decide a seat are never broken silently: they are reported in ``ties`` and,
where the algorithm must continue (IRV/STV eliminations), resolved by a
documented deterministic lot (SHA-256 of election id + candidate id) that
anyone re-running the count reproduces exactly.
"""
import hashlib
from fractions import Fraction


def _pct(part, whole):
    return round(100.0 * part / whole, 2) if whole else 0.0


def _lot_key(seed, candidate_id):
    return hashlib.sha256(f'{seed}:{candidate_id}'.encode()).hexdigest()


def _rank(counts, candidates):
    """Candidates ordered by votes desc, then name for stable display."""
    return sorted(candidates, key=lambda c: (-counts.get(c['id'], 0), c['name'].lower(), c['id']))


def _winners_with_ties(ordered, counts, seats):
    if seats <= 0 or not ordered:
        return [], []
    winners = ordered[:seats]
    cutoff = counts.get(winners[-1]['id'], 0)
    tied_at_cutoff = [c for c in ordered if counts.get(c['id'], 0) == cutoff]
    if len(tied_at_cutoff) > 1 and any(c not in winners for c in tied_at_cutoff):
        safe = [c for c in winners if counts.get(c['id'], 0) > cutoff]
        return safe, [c['id'] for c in tied_at_cutoff]
    return winners, []


def _candidate_rows(ordered, counts, valid_votes):
    return [{'candidate_id': c['id'], 'name': c['name'], 'votes': counts.get(c['id'], 0),
             'percentage': _pct(counts.get(c['id'], 0), valid_votes), 'rank': i + 1}
            for i, c in enumerate(ordered)]


def plurality(selections, candidates, seats=1):
    """Single choice, FPTP (block plurality), multiple choice and approval."""
    counts = {c['id']: 0 for c in candidates}
    abstentions = valid = 0
    for selection in selections:
        if not selection:
            abstentions += 1
            continue
        valid += 1
        for cid in selection:
            if cid in counts:
                counts[cid] += 1
    total_marks = sum(counts.values())
    ordered = _rank(counts, candidates)
    winners, ties = _winners_with_ties(ordered, counts, seats)
    rows = _candidate_rows(ordered, counts, total_marks)
    for row in rows:
        row['percentage_of_ballots'] = _pct(row['votes'], valid)
    return {
        'method': 'plurality', 'seats': seats, 'valid_ballots': valid, 'abstentions': abstentions,
        'total_votes': total_marks, 'candidates': rows,
        'winners': [c['id'] for c in winners], 'ties': ties,
    }


def score(selections, candidates, seats=1, max_score=10):
    totals = {c['id']: 0 for c in candidates}
    ballots_scoring = {c['id']: 0 for c in candidates}
    abstentions = valid = 0
    for selection in selections:
        if not selection:
            abstentions += 1
            continue
        valid += 1
        for cid, value in selection.items():
            cid = int(cid)
            if cid in totals:
                totals[cid] += int(value)
                ballots_scoring[cid] += 1
    ordered = _rank(totals, candidates)
    winners, ties = _winners_with_ties(ordered, totals, seats)
    rows = []
    for i, c in enumerate(ordered):
        rows.append({'candidate_id': c['id'], 'name': c['name'], 'votes': totals[c['id']],
                     'average': round(totals[c['id']] / valid, 3) if valid else 0.0,
                     'percentage': _pct(totals[c['id']], max_score * valid), 'rank': i + 1,
                     'ballots_scoring': ballots_scoring[c['id']]})
    return {'method': 'score', 'seats': seats, 'max_score': max_score, 'valid_ballots': valid,
            'abstentions': abstentions, 'total_votes': sum(totals.values()), 'candidates': rows,
            'winners': [c['id'] for c in winners], 'ties': ties}


def referendum(selections, threshold=50.0):
    yes = sum(1 for s in selections if s == 'YES')
    no = sum(1 for s in selections if s == 'NO')
    abstentions = sum(1 for s in selections if not s)
    valid = yes + no
    yes_pct = _pct(yes, valid)
    passed = valid > 0 and yes_pct > float(threshold)
    return {'method': 'referendum', 'valid_ballots': valid, 'abstentions': abstentions, 'total_votes': valid,
            'yes': yes, 'no': no, 'yes_percentage': yes_pct, 'no_percentage': _pct(no, valid),
            'threshold': float(threshold), 'passed': passed,
            'candidates': [
                {'candidate_id': 'YES', 'name': 'Yes', 'votes': yes, 'percentage': yes_pct, 'rank': 1 if yes >= no else 2},
                {'candidate_id': 'NO', 'name': 'No', 'votes': no, 'percentage': _pct(no, valid), 'rank': 1 if no > yes else 2},
            ],
            'winners': ['YES'] if passed else ['NO'], 'ties': ['YES', 'NO'] if valid and yes == no else []}


def ranked(selections, candidates, seats=1, seed='0'):
    """Instant-runoff (1 seat) or single transferable vote (Droop quota,
    Gregory fractional surplus transfers) for multi-seat positions."""
    hopeful = {c['id'] for c in candidates}
    names = {c['id']: c['name'] for c in candidates}
    ballots = []
    abstentions = 0
    for selection in selections:
        prefs = [cid for cid in (selection or []) if cid in hopeful]
        if not prefs:
            abstentions += 1
            continue
        ballots.append([prefs, Fraction(1)])
    valid = len(ballots)
    first_prefs = {cid: 0 for cid in hopeful}
    for prefs, _ in ballots:
        first_prefs[prefs[0]] += 1

    seats = max(1, min(seats, len(hopeful))) if hopeful else 0
    quota = Fraction(valid, seats + 1) if seats > 1 else None
    elected, eliminated, rounds = [], [], []
    tie_breaks = []
    continuing = set(hopeful)

    def current_tallies():
        tallies = {cid: Fraction(0) for cid in continuing}
        exhausted = Fraction(0)
        for prefs, weight in ballots:
            for cid in prefs:
                if cid in continuing:
                    tallies[cid] += weight
                    break
            else:
                exhausted += weight
        return tallies, exhausted

    def lot_order(cids):
        return sorted(cids, key=lambda cid: (_lot_key(seed, cid)))

    while continuing and len(elected) < seats:
        tallies, exhausted = current_tallies()
        active_total = sum(tallies.values())
        snapshot = {'round': len(rounds) + 1,
                    'tallies': {str(cid): float(round(v, 4)) for cid, v in sorted(tallies.items())},
                    'exhausted': float(round(exhausted, 4))}
        if len(continuing) <= seats - len(elected):
            newly = sorted(continuing, key=lambda cid: (-tallies[cid], _lot_key(seed, cid)))
            elected.extend(newly)
            snapshot['elected'] = newly
            snapshot['note'] = 'Remaining candidates fill the remaining seats.'
            rounds.append(snapshot)
            break
        threshold = quota if quota is not None else active_total / 2
        winners_now = [cid for cid in continuing if (tallies[cid] >= threshold if quota is not None
                                                     else tallies[cid] > threshold)]
        if winners_now:
            winners_now.sort(key=lambda cid: (-tallies[cid], _lot_key(seed, cid)))
            winner = winners_now[0]
            elected.append(winner)
            continuing.discard(winner)
            snapshot['elected'] = [winner]
            if quota is not None and tallies[winner] > quota:
                surplus_ratio = (tallies[winner] - quota) / tallies[winner]
                for ballot in ballots:
                    prefs = ballot[0]
                    top = next((cid for cid in prefs if cid in continuing or cid == winner), None)
                    if top == winner:
                        ballot[1] *= surplus_ratio
                snapshot['surplus_transferred'] = float(round(tallies[winner] - quota, 4))
            elif quota is not None:
                for ballot in ballots:
                    top = next((cid for cid in ballot[0] if cid in continuing or cid == winner), None)
                    if top == winner:
                        ballot[1] = Fraction(0)
            rounds.append(snapshot)
            continue
        lowest = min(tallies[cid] for cid in continuing)
        candidates_lowest = [cid for cid in continuing if tallies[cid] == lowest]
        if len(candidates_lowest) > 1:
            # Tie for elimination: fewest first preferences, then deterministic lot.
            fewest_first = min(first_prefs[cid] for cid in candidates_lowest)
            narrowed = [cid for cid in candidates_lowest if first_prefs[cid] == fewest_first]
            loser = lot_order(narrowed)[0]
            tie_breaks.append({'round': snapshot['round'], 'tied': sorted(candidates_lowest), 'eliminated': loser,
                               'method': 'fewest first preferences, then lot' if len(narrowed) > 1 else 'fewest first preferences'})
        else:
            loser = candidates_lowest[0]
        continuing.discard(loser)
        eliminated.append(loser)
        snapshot['eliminated'] = [loser]
        rounds.append(snapshot)

    final_tallies, _ = current_tallies() if continuing else ({}, 0)
    order = elected + [cid for cid in sorted(continuing, key=lambda c: -final_tallies.get(c, 0))] + list(reversed(eliminated))
    rows = [{'candidate_id': cid, 'name': names[cid], 'votes': first_prefs.get(cid, 0),
             'percentage': _pct(first_prefs.get(cid, 0), valid), 'rank': i + 1, 'elected': cid in elected}
            for i, cid in enumerate(order)]
    return {
        'method': 'stv' if seats > 1 else 'irv', 'seats': seats, 'valid_ballots': valid, 'abstentions': abstentions,
        'total_votes': valid, 'quota': float(round(quota, 4)) if quota is not None else None,
        'first_preferences': {str(k): v for k, v in sorted(first_prefs.items())},
        'rounds': rounds, 'candidates': rows, 'winners': elected, 'ties': [], 'tie_breaks': tie_breaks,
    }


def tally_position(position_spec, selections, seed='0'):
    bt = position_spec['ballot_type']
    candidates = position_spec['candidates']
    seats = position_spec.get('seats', 1) or 1
    if bt == 'REFERENDUM':
        result = referendum(selections, position_spec.get('referendum_threshold', 50))
    elif bt == 'RANKED':
        result = ranked(selections, candidates, seats=seats, seed=seed)
    elif bt == 'SCORE':
        result = score(selections, candidates, seats=seats, max_score=position_spec.get('max_score', 10))
    else:
        result = plurality(selections, candidates, seats=1 if bt == 'SINGLE' else seats)
    result.update({'position_id': position_spec['id'], 'position': position_spec['name'], 'ballot_type': bt})
    return result
