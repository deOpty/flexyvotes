from django.test import SimpleTestCase

from elections import tally

CANDS = [{'id': 1, 'name': 'Alice'}, {'id': 2, 'name': 'Bob'}, {'id': 3, 'name': 'Chidi'}, {'id': 4, 'name': 'Dede'}]


class PluralityTests(SimpleTestCase):
    def test_single_choice_counts_percentages_and_winner(self):
        result = tally.plurality([[1], [1], [2], None, [3], [1]], CANDS[:3])
        self.assertEqual(result['valid_ballots'], 5)
        self.assertEqual(result['abstentions'], 1)
        rows = {r['candidate_id']: r for r in result['candidates']}
        self.assertEqual(rows[1]['votes'], 3)
        self.assertEqual(rows[1]['percentage'], 60.0)
        self.assertEqual(result['winners'], [1])
        self.assertEqual(result['ties'], [])

    def test_tie_for_the_seat_is_reported_not_broken(self):
        result = tally.plurality([[1], [2]], CANDS[:2])
        self.assertEqual(result['winners'], [])
        self.assertEqual(sorted(result['ties']), [1, 2])

    def test_multi_seat_block_voting(self):
        result = tally.plurality([[1, 2], [1, 3], [2, 3], [1, 2]], CANDS[:3], seats=2)
        self.assertEqual(sorted(result['winners']), [1, 2])

    def test_unknown_candidate_ids_are_ignored(self):
        result = tally.plurality([[99], [1]], CANDS[:2])
        self.assertEqual({r['candidate_id']: r['votes'] for r in result['candidates']}, {1: 1, 2: 0})


class RankedTests(SimpleTestCase):
    def test_irv_majority_in_first_round(self):
        result = tally.ranked([[1, 2], [1, 3], [2, 1]], CANDS[:3])
        self.assertEqual(result['method'], 'irv')
        self.assertEqual(result['winners'], [1])
        self.assertEqual(len(result['rounds']), 1)

    def test_irv_transfers_after_elimination(self):
        # First prefs: A=4, B=3, C=2 -> C eliminated, C's ballots go to B -> B wins 5-4.
        ballots = [[1]] * 4 + [[2]] * 3 + [[3, 2]] * 2
        result = tally.ranked(ballots, CANDS[:3])
        self.assertEqual(result['winners'], [2])
        self.assertEqual(result['rounds'][0]['eliminated'], [3])

    def test_irv_exhausted_ballots_do_not_count_toward_majority(self):
        ballots = [[1]] * 3 + [[2]] * 2 + [[3]] * 2
        result = tally.ranked(ballots, CANDS[:3])
        self.assertIn(result['winners'][0], (1, 2, 3))
        self.assertGreater(result['rounds'][-1]['exhausted'], 0)

    def test_stv_two_seats_with_surplus_transfer(self):
        # Droop quota for 9 ballots / 2 seats = 3. Alice has 6 -> surplus 3 flows to Bob.
        ballots = [[1, 2]] * 6 + [[3]] * 2 + [[4]] * 1
        result = tally.ranked(ballots, CANDS, seats=2)
        self.assertEqual(result['method'], 'stv')
        self.assertEqual(result['quota'], 3.0)
        self.assertEqual(result['winners'], [1, 2])

    def test_deterministic_lot_is_reproducible(self):
        ballots = [[1], [2], [3, 1], [4, 2]]
        a = tally.ranked(ballots, CANDS, seed='42')
        b = tally.ranked(list(reversed(ballots)), CANDS, seed='42')
        self.assertEqual(a['winners'], b['winners'])


class ScoreAndReferendumTests(SimpleTestCase):
    def test_score_totals_and_average(self):
        result = tally.score([{'1': 5, '2': 3}, {'1': 4, '2': 5}, None], CANDS[:2], max_score=5)
        rows = {r['candidate_id']: r for r in result['candidates']}
        self.assertEqual(rows[1]['votes'], 9)
        self.assertEqual(rows[2]['votes'], 8)
        self.assertEqual(result['winners'], [1])
        self.assertEqual(result['abstentions'], 1)

    def test_referendum_passes_only_above_threshold(self):
        self.assertTrue(tally.referendum(['YES', 'YES', 'NO'], 50)['passed'])
        self.assertFalse(tally.referendum(['YES', 'NO'], 50)['passed'])
        self.assertFalse(tally.referendum(['YES', 'YES', 'NO'], 66.67)['passed'])
        result = tally.referendum(['YES', None, 'NO', 'NO'], 50)
        self.assertEqual(result['abstentions'], 1)
        self.assertEqual(result['no'], 2)

    def test_tally_position_dispatches_by_type(self):
        spec = {'id': 7, 'name': 'Constitution', 'ballot_type': 'REFERENDUM', 'candidates': [], 'referendum_threshold': 50}
        result = tally.tally_position(spec, ['YES'])
        self.assertEqual(result['position_id'], 7)
        self.assertEqual(result['method'], 'referendum')
