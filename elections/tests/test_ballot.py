from django.http import QueryDict
from django.test import TestCase

from core.tests.factories import add_position, make_event
from elections.ballot import BallotError, configuration_problems, describe_selections, validate_ballot
from elections.casting import selections_from_post
from voting.models import Category

BT = Category.BallotType


class BallotValidationTests(TestCase):
    def setUp(self):
        self.event = make_event()
        self.single, self.single_c = add_position(self.event, 'President')
        self.multi, self.multi_c = add_position(self.event, 'Senate', BT.MULTIPLE, ('D', 'E', 'F'), min_select=1, max_select=2)
        self.ranked, self.ranked_c = add_position(self.event, 'Treasurer', BT.RANKED, ('G', 'H', 'I'), max_select=3)
        self.score, self.score_c = add_position(self.event, 'Award', BT.SCORE, ('J', 'K'), max_score=5)
        self.ref, _ = add_position(self.event, 'Amendment', BT.REFERENDUM, (), allow_abstain=False)
        self.style = [p.pk for p in (self.single, self.multi, self.ranked, self.score, self.ref)]

    def payload(self, **overrides):
        data = {str(self.single.pk): [self.single_c[0].pk], str(self.multi.pk): [self.multi_c[0].pk],
                str(self.ranked.pk): [self.ranked_c[2].pk, self.ranked_c[0].pk],
                str(self.score.pk): {str(self.score_c[0].pk): 4}, str(self.ref.pk): 'yes'}
        data.update({str(k): v for k, v in overrides.items()})
        return data

    def test_valid_ballot_normalizes_every_type(self):
        result = validate_ballot(self.event, self.style, self.payload())
        self.assertEqual(result[str(self.ranked.pk)], [self.ranked_c[2].pk, self.ranked_c[0].pk])
        self.assertEqual(result[str(self.ref.pk)], 'YES')
        self.assertEqual(result[str(self.score.pk)], {str(self.score_c[0].pk): 4})

    def test_rejects_too_many_selections(self):
        with self.assertRaisesMessage(BallotError, 'at most 2'):
            validate_ballot(self.event, self.style, self.payload(**{str(self.multi.pk): [c.pk for c in self.multi_c]}))

    def test_rejects_duplicate_and_foreign_candidates(self):
        with self.assertRaises(BallotError):
            validate_ballot(self.event, self.style, self.payload(**{str(self.multi.pk): [self.multi_c[0].pk] * 2}))
        with self.assertRaises(BallotError):
            validate_ballot(self.event, self.style, self.payload(**{str(self.single.pk): [self.multi_c[0].pk]}))

    def test_abstention_rules(self):
        result = validate_ballot(self.event, self.style, self.payload(**{str(self.single.pk): []}))
        self.assertIsNone(result[str(self.single.pk)])
        with self.assertRaises(BallotError):
            validate_ballot(self.event, self.style, self.payload(**{str(self.ref.pk): ''}))

    def test_score_out_of_range_rejected(self):
        with self.assertRaisesMessage(BallotError, 'between 0 and 5'):
            validate_ballot(self.event, self.style, self.payload(**{str(self.score.pk): {str(self.score_c[0].pk): 9}}))

    def test_positions_outside_ballot_style_rejected(self):
        with self.assertRaisesMessage(BallotError, 'not eligible'):
            validate_ballot(self.event, [self.single.pk], self.payload())

    def test_withdrawn_candidates_cannot_be_selected(self):
        self.single_c[0].status = 'WITHDRAWN'
        self.single_c[0].save()
        with self.assertRaises(BallotError):
            validate_ballot(self.event, self.style, self.payload())

    def test_form_post_parsing_and_rank_gaps(self):
        post = QueryDict(mutable=True)
        post.setlist(f'pos_{self.multi.pk}', [str(self.multi_c[1].pk)])
        post[f'pos_{self.ranked.pk}_rank_{self.ranked_c[0].pk}'] = '2'
        post[f'pos_{self.ranked.pk}_rank_{self.ranked_c[1].pk}'] = '1'
        post[f'pos_{self.score.pk}_score_{self.score_c[1].pk}'] = '3'
        post[f'pos_{self.ref.pk}'] = 'NO'
        post[f'pos_{self.single.pk}_abstain'] = 'on'
        payload = selections_from_post(self.event, self.style, post)
        self.assertEqual(payload[str(self.ranked.pk)], [str(self.ranked_c[1].pk), str(self.ranked_c[0].pk)])
        self.assertIsNone(payload[str(self.single.pk)])
        validated = validate_ballot(self.event, self.style, payload)
        rows = describe_selections(self.event, validated)
        self.assertEqual(rows[0]['choices'], ['Abstained'])
        post[f'pos_{self.ranked.pk}_rank_{self.ranked_c[1].pk}'] = '3'
        with self.assertRaisesMessage(BallotError, 'no gaps'):
            selections_from_post(self.event, self.style, post)

    def test_configuration_problems(self):
        event = make_event(title='Empty')
        self.assertTrue(configuration_problems(event))
        add_position(event, 'Solo', BT.MULTIPLE, ('A',), min_select=2, max_select=2)
        problems = configuration_problems(event)
        self.assertTrue(any('requires 2 selections' in p for p in problems))
        self.assertTrue(any('voter roll' in p for p in problems))
