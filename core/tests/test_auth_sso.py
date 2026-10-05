import re
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pyotp
import responses
from cryptography.hazmat.primitives.asymmetric import rsa
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from core import auth
from core.models import KnownDevice, UserSecurity, UserSession
from core.tests.factories import PASSWORD, institutional_setup, make_user, open_institutional
from elections.models import Voter


class StaffAuthTests(TestCase):
    def setUp(self):
        self.user = make_user('staffer')

    def login(self, password=PASSWORD, **extra):
        return self.client.post(reverse('login'), {'username': 'staffer', 'password': password}, **extra)

    def test_lockout_after_repeated_failures(self):
        with self.captureOnCommitCallbacks(execute=True):
            for _ in range(5):
                self.login(password='wrong-password')
        security = UserSecurity.objects.get(user=self.user)
        self.assertTrue(security.is_locked)
        response = self.login()
        self.assertEqual(response.status_code, 429)
        self.assertTrue(any('locked' in m.subject.lower() or 'locked' in m.body.lower() for m in mail.outbox))

    def test_totp_enrolment_and_second_step(self):
        self.client.login(username='staffer', password=PASSWORD)
        self.client.post(reverse('account:security_action'), {'action': 'totp_start'})
        secret = self.client.session['fv_totp_setup']
        page = self.client.get(reverse('account:security'))
        self.assertContains(page, 'data:image/png;base64')
        self.client.post(reverse('account:security_action'), {'action': 'totp_confirm', 'code': pyotp.TOTP(secret).now()})
        security = UserSecurity.objects.get(user=self.user)
        self.assertTrue(security.totp_enabled)
        self.assertEqual(len(security.recovery_codes), 10)
        codes = self.client.get(reverse('account:security')).context['recovery_codes']
        self.client.post(reverse('logout'))

        response = self.login()
        self.assertRedirects(response, reverse('account:mfa'))
        self.assertNotIn('_auth_user_id', self.client.session)
        self.client.post(reverse('account:mfa'), {'method': 'totp', 'code': '000000'})
        self.assertNotIn('_auth_user_id', self.client.session)
        response = self.client.post(reverse('account:mfa'), {'method': 'recovery', 'code': codes[0]})
        self.assertEqual(response.status_code, 302)
        self.assertIn('_auth_user_id', self.client.session)
        self.assertEqual(len(UserSecurity.objects.get(user=self.user).recovery_codes), 9)

    def test_totp_codes_cannot_be_replayed(self):
        secret = auth.new_totp_secret()
        code = pyotp.TOTP(secret).now()
        self.assertTrue(auth.verify_totp(self.user, code, secret=secret))
        self.assertFalse(auth.verify_totp(self.user, code, secret=secret))

    def test_new_device_triggers_alert_and_email_step_up(self):
        KnownDevice.objects.create(user=self.user, device_hash='some-other-device')
        with self.captureOnCommitCallbacks(execute=True):
            response = self.login()
        self.assertRedirects(response, reverse('account:mfa'))
        code = re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('account:mfa'), {'method': 'email', 'code': code})
        self.assertIn('_auth_user_id', self.client.session)
        self.assertEqual(KnownDevice.objects.filter(user=self.user).count(), 2)

    def test_session_listing_and_remote_revocation(self):
        other = self.client_class()
        other.login(username='staffer', password=PASSWORD)
        other.get(reverse('account:security'))
        self.client.login(username='staffer', password=PASSWORD)
        self.client.get(reverse('account:security'))
        self.assertEqual(UserSession.objects.filter(user=self.user, revoked=False).count(), 2)
        self.client.post(reverse('account:security_action'), {'action': 'sessions_revoke_others'})
        response = other.get(reverse('account:security'))
        self.assertEqual(response.status_code, 302)

    @override_settings(ENFORCE_STAFF_MFA=True)
    def test_mfa_enrolment_enforced_for_console_users(self):
        staff = make_user('boss', staff=True)
        self.client.login(username='boss', password=PASSWORD)
        response = self.client.get(reverse('dashboard'))
        self.assertTrue(response['Location'].startswith('/account/security/'))
        self.assertEqual(staff.security.mfa_enabled, False)

    def test_api_token_lifecycle(self):
        raw, token = auth.create_api_token(self.user, 'ci')
        self.assertTrue(raw.startswith('fv_'))
        self.assertNotIn(raw, token.token_hash)
        self.assertEqual(auth.authenticate_api_token(raw).user, self.user)
        self.assertIsNone(auth.authenticate_api_token('fv_wrong'))

    def test_registration_rejects_weak_password_and_bots(self):
        response = self.client.post(reverse('register'), {'username': 'newbie', 'email': 'n@example.com', 'password': '123'})
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse('register'), {'username': 'botty', 'email': 'b@example.com',
                                                          'password': 'a-strong-pass-123', 'website': 'spam'})
        from django.contrib.auth import get_user_model

        self.assertFalse(get_user_model().objects.filter(username__in=['newbie', 'botty']).exists())
        response = self.client.post(reverse('register'), {'username': 'goodorg', 'email': 'g@example.com',
                                                          'password': 'a-strong-pass-123'})
        self.assertRedirects(response, reverse('home'))


def _rsa_jwk():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    public_jwk.update({'kid': 'test-key', 'alg': 'RS256', 'use': 'sig'})
    return key, public_jwk


@override_settings(SSO_PROVIDERS={'google': {'name': 'Google', 'issuer': 'https://accounts.google.com',
                                             'client_id': 'client-123', 'client_secret': 'shh'}},
                   SITE_URL='https://vote.example.com')
class OidcTests(TestCase):
    def setUp(self):
        self.key, self.jwk = _rsa_jwk()
        responses.start()
        responses.add(responses.GET, 'https://accounts.google.com/.well-known/openid-configuration', json={
            'issuer': 'https://accounts.google.com',
            'authorization_endpoint': 'https://accounts.google.com/o/oauth2/v2/auth',
            'token_endpoint': 'https://oauth2.googleapis.com/token',
            'jwks_uri': 'https://www.googleapis.com/oauth2/v3/certs'})
        responses.add(responses.GET, 'https://www.googleapis.com/oauth2/v3/certs', json={'keys': [self.jwk]})

    def tearDown(self):
        responses.stop()
        responses.reset()

    def start(self, url):
        response = self.client.get(url)
        params = parse_qs(urlparse(response['Location']).query)
        return params['state'][0], params['nonce'][0], params

    def id_token(self, nonce, email, audience='client-123', verified=True, sub='google-sub-1'):
        now = int(time.time())
        claims = {'iss': 'https://accounts.google.com', 'aud': audience, 'sub': sub, 'iat': now,
                  'exp': now + 300, 'nonce': nonce, 'email': email, 'email_verified': verified, 'name': 'Test'}
        return jwt.encode(claims, self.key, algorithm='RS256', headers={'kid': 'test-key'})

    def finish(self, state, token, provider='google'):
        responses.add(responses.POST, 'https://oauth2.googleapis.com/token', json={'id_token': token})
        return self.client.get(reverse('sso_callback', args=[provider]) + f'?state={state}&code=authcode')

    def test_staff_sso_login(self):
        user = make_user('ssouser', email='sso@example.com')
        state, nonce, params = self.start(reverse('sso_start', args=['google']))
        self.assertEqual(params['code_challenge_method'], ['S256'])
        response = self.finish(state, self.id_token(nonce, 'sso@example.com'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), user.pk)

    def test_wrong_nonce_audience_or_state_rejected(self):
        make_user('ssouser', email='sso@example.com')
        state, nonce, _ = self.start(reverse('sso_start', args=['google']))
        self.finish(state, self.id_token('not-the-nonce', 'sso@example.com'))
        self.assertNotIn('_auth_user_id', self.client.session)
        state, nonce, _ = self.start(reverse('sso_start', args=['google']))
        self.finish(state, self.id_token(nonce, 'sso@example.com', audience='someone-else'))
        self.assertNotIn('_auth_user_id', self.client.session)
        response = self.client.get(reverse('sso_callback', args=['google']) + '?state=forged&code=x')
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(response.status_code, 302)

    def test_voter_sso_login_matches_roll_by_verified_email(self):
        s = institutional_setup()
        event = open_institutional(s['event'], s['admin'], s['reviewer'])
        event.auth_methods = ['SSO']
        event.save(update_fields=['auth_methods'])
        voter = s['voters'][0]
        state, nonce, _ = self.start(reverse('sso_start', args=['google']) + f'?event={event.pk}')
        response = self.finish(state, self.id_token(nonce, voter.email))
        self.assertRedirects(response, reverse('elections:vote_ballot', args=[event.pk]), fetch_redirect_response=False)
        self.assertTrue(Voter.objects.get(pk=voter.pk).sso_subject_index)
        # An unverified email is never trusted.
        other = self.client_class()
        self.client = other
        state, nonce, _ = self.start(reverse('sso_start', args=['google']) + f'?event={event.pk}')
        response = self.finish(state, self.id_token(nonce, s['voters'][1].email, verified=False, sub='google-sub-2'))
        self.assertNotEqual(response.get('Location'), reverse('elections:vote_ballot', args=[event.pk]))
