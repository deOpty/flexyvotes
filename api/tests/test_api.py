import json
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from core.auth import create_api_token
from core.tests.factories import PASSWORD, add_position, institutional_setup, make_event, make_org, make_user, open_institutional
from elections.models import Ballot
from voting.models import Event


class ApiTests(TestCase):
    def setUp(self):
        self.s = institutional_setup()
        raw, _ = create_api_token(self.s['admin'], 'tests')
        self.auth = {'HTTP_AUTHORIZATION': f'Bearer {raw}'}

    def get(self, path, **extra):
        return self.client.get(f'/api/v1{path}', **{**self.auth, **extra})

    def post(self, path, data, **extra):
        return self.client.post(f'/api/v1{path}', json.dumps(data), content_type='application/json', **{**self.auth, **extra})

    def test_openapi_schema_and_docs(self):
        schema = self.client.get('/api/v1/openapi.json').json()
        self.assertIn('/api/v1/elections/{event_id}/vote', schema['paths'])
        self.assertEqual(self.client.get('/api/v1/docs').status_code, 200)

    def test_authentication_and_error_envelope(self):
        response = self.client.get('/api/v1/auth/me')
        self.assertEqual(response.status_code, 401)
        error = response.json()['error']
        self.assertEqual(error['code'], 'unauthenticated')
        self.assertIn('correlation_id', error)
        me = self.get('/auth/me').json()
        self.assertEqual(me['username'], 'admin1')

    def test_password_token_exchange(self):
        response = self.client.post('/api/v1/auth/token', json.dumps({'username': 'admin1', 'password': PASSWORD}),
                                    content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['token'].startswith('fv_'))
        bad = self.client.post('/api/v1/auth/token', json.dumps({'username': 'admin1', 'password': 'nope'}),
                               content_type='application/json')
        self.assertEqual(bad.status_code, 401)

    def test_elections_are_tenant_scoped(self):
        other = make_user('other')
        other_org = make_org('Other', admin=other)
        make_event(org=other_org, title='Secret other election')
        titles = [e['title'] for e in self.get('/elections').json()['items']]
        self.assertEqual(titles, [self.s['event'].title])
        other_event = Event.objects.get(title='Secret other election')
        self.assertEqual(self.get(f'/elections/{other_event.pk}/voters').status_code, 403)

    def test_create_election_positions_candidates_voters_and_transition(self):
        now = timezone.now()
        created = self.post('/elections', {'title': 'API Election', 'mode': 'INSTITUTIONAL',
                                           'start_date': now.isoformat(), 'end_date': (now + timedelta(days=1)).isoformat()})
        self.assertEqual(created.status_code, 201, created.content)
        event_id = created.json()['id']
        position = self.post(f'/elections/{event_id}/positions', {'name': 'Chair', 'ballot_type': 'SINGLE'})
        self.assertEqual(position.status_code, 201)
        for name in ('Ann', 'Ben'):
            self.assertEqual(self.post(f'/elections/{event_id}/candidates',
                                       {'name': name, 'position_id': position.json()['id']}).status_code, 201)
        report = self.post(f'/elections/{event_id}/voters', [{'identifier': 'API1', 'email': 'api1@uni.edu'}]).json()
        self.assertEqual(report['created'], 1)
        voters = self.get(f'/elections/{event_id}/voters').json()['items']
        self.assertEqual(voters[0]['email_masked'], 'a***@uni.edu')
        response = self.post(f'/elections/{event_id}/transitions', {'action': 'submit'})
        self.assertEqual(response.json()['status'], 'REVIEW')
        response = self.post(f'/elections/{event_id}/transitions', {'action': 'publish'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error']['code'], 'invalid_transition')

    def test_validation_errors_use_envelope(self):
        response = self.post('/elections', {'title': ''})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'validation_error')

    def test_voter_ballot_and_idempotent_vote(self):
        event = open_institutional(self.s['event'], self.s['admin'], self.s['reviewer'])
        voter = self.s['voters'][0]
        session = self.client.post(f'/api/v1/elections/{event.pk}/ballot/session',
                                   json.dumps({'code': self.s['codes'][voter.pk]}), content_type='application/json')
        self.assertEqual(session.status_code, 200, session.content)
        token = session.json()['ballot_token']
        headers = {'HTTP_AUTHORIZATION': f'Bearer {token}', 'HTTP_IDEMPOTENCY_KEY': 'vote-key-0001'}
        ballot = self.client.get(f'/api/v1/elections/{event.pk}/ballot', **headers).json()
        self.assertEqual({p['name'] for p in ballot}, {'President', 'Senate'})
        payload = json.dumps({'selections': {str(self.s['president'].pk): [self.s['president_candidates'][0].pk],
                                             str(self.s['senate'].pk): []}})
        first = self.client.post(f'/api/v1/elections/{event.pk}/vote', payload, content_type='application/json', **headers)
        self.assertEqual(first.status_code, 200, first.content)
        replay = self.client.post(f'/api/v1/elections/{event.pk}/vote', payload, content_type='application/json', **headers)
        self.assertEqual(replay.json()['tracker'], first.json()['tracker'])
        self.assertEqual(replay['Idempotent-Replay'], 'true')
        self.assertEqual(Ballot.objects.count(), 1)
        headers['HTTP_IDEMPOTENCY_KEY'] = 'vote-key-0002'
        again = self.client.post(f'/api/v1/elections/{event.pk}/vote', payload, content_type='application/json', **headers)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()['error']['code'], 'already_voted')
        bad = self.client.post(f'/api/v1/elections/{event.pk}/ballot/session', json.dumps({'code': 'WRONG'}),
                               content_type='application/json')
        self.assertEqual(bad.status_code, 401)

    def test_results_hidden_until_published(self):
        event = open_institutional(self.s['event'], self.s['admin'], self.s['reviewer'])
        response = self.client.get(f'/api/v1/elections/{event.pk}/results')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(f'/api/v1/elections/{event.pk}/turnout').status_code, 403)
        self.assertEqual(self.get(f'/elections/{event.pk}/turnout').status_code, 200)

    def test_audit_endpoints(self):
        open_institutional(self.s['event'], self.s['admin'], self.s['reviewer'])
        entries = self.get('/audit').json()['items']
        self.assertTrue(any(e['event_type'] == 'ELECTION_APPROVE' for e in entries))
        verify = self.get('/audit/verify').json()
        self.assertTrue(all(v['ok'] for v in verify.values()))


@override_settings(PAYMENTS_FAKE_GATEWAY=True, DEBUG=True)
class ApiPaymentTests(TestCase):
    def test_payment_requires_idempotency_key_and_replays(self):
        event = make_event(institutional=False, status=Event.Status.OPEN)
        _, (alice, *_rest) = add_position(event, 'Best Actor')
        url = f'/api/v1/elections/{event.pk}/payments'
        body = json.dumps({'candidate_id': alice.pk, 'votes': 2, 'email': 'fan@gmail.com'})
        self.assertEqual(self.client.post(url, body, content_type='application/json').status_code, 400)
        first = self.client.post(url, body, content_type='application/json', HTTP_IDEMPOTENCY_KEY='pay-key-0001')
        self.assertEqual(first.status_code, 201, first.content)
        second = self.client.post(url, body, content_type='application/json', HTTP_IDEMPOTENCY_KEY='pay-key-0001')
        self.assertEqual(first.json()['reference'], second.json()['reference'])
        status = self.client.get(f"/api/v1/payments/{first.json()['reference']}").json()
        self.assertEqual(status['status'], 'PENDING')
        quote = self.client.post(f'/api/v1/elections/{event.pk}/payments/quote',
                                 json.dumps({'candidate_id': alice.pk, 'votes': 3}), content_type='application/json')
        self.assertEqual(quote.json()['amount'], '3.00')
