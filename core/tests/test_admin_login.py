"""Admins can sign in wherever the site is served - including plain
http://<ip>:<port> before TLS - and the sign-in checks never shut out the
right password for reasons the admin cannot fix (undeliverable email codes,
container restarts). Also the browser-console errors on those pages."""
import io
import os
import re
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditEvent, KnownDevice, OTPChallenge, UserSecurity
from core.tests.factories import PASSWORD, make_user

HTTP_IP = {'HTTP_HOST': '16.192.12.135:5000'}  # how the production server is reached today


def secure_flag(response, name):
    return bool(response.cookies[name]['secure'])


class CookieModeTests(TestCase):
    """COOKIE_SECURE=auto (default): Secure on HTTPS, usable over plain HTTP."""

    def setUp(self):
        self.admin = make_user('platform', staff=True, superuser=True)

    def sign_in(self, client, **extra):
        page = client.get(reverse('login') + '?next=/console/elections/', **extra)
        token = page.cookies['fv_csrftoken'].value
        response = client.post(reverse('login') + '?next=/console/elections/',
                               {'username': 'platform', 'password': PASSWORD, 'csrfmiddlewaretoken': token}, **extra)
        return page, response

    def test_default_mode_is_auto(self):
        from django.conf import settings

        self.assertEqual(settings.COOKIE_SECURE_MODE, 'auto')

    def test_plain_http_sign_in_gets_cookies_the_browser_keeps(self):
        client = Client(enforce_csrf_checks=True)
        page, response = self.sign_in(client, **HTTP_IP)
        self.assertFalse(secure_flag(page, 'fv_csrftoken'))
        self.assertFalse(secure_flag(page, 'fv_did'))
        self.assertRedirects(response, '/console/elections/', fetch_redirect_response=False)
        self.assertFalse(secure_flag(response, 'fv_session'))
        self.assertEqual(client.get('/console/elections/', **HTTP_IP).status_code, 200)

    def test_https_and_proxied_https_keep_secure_cookies(self):
        # Over HTTPS Django's CSRF check also wants the Referer a browser sends.
        page, response = self.sign_in(Client(enforce_csrf_checks=True), secure=True, HTTP_HOST='vote.example.com',
                                      HTTP_REFERER='https://vote.example.com/login/')
        self.assertTrue(secure_flag(page, 'fv_csrftoken'))
        self.assertTrue(secure_flag(response, 'fv_session'))
        # Behind nginx / an ALB the TLS hop is reported in X-Forwarded-Proto.
        page = Client().get(reverse('login'), HTTP_HOST='vote.example.com', HTTP_X_FORWARDED_PROTO='https')
        self.assertTrue(secure_flag(page, 'fv_csrftoken'))
        self.assertTrue(secure_flag(page, 'fv_did'))

    @override_settings(COOKIE_SECURE_MODE='always')
    def test_forced_secure_cookies_explain_why_http_sign_in_fails(self):
        page = self.client.get(reverse('login'), **HTTP_IP)
        self.assertTrue(secure_flag(page, 'fv_csrftoken'))
        self.assertContains(page, "Sign-in can't work on this address")
        self.assertContains(page, 'COOKIE_SECURE=auto')
        # The browser dropped the cookie, so the form arrives without one.
        response = Client(enforce_csrf_checks=True).post(
            reverse('login'), {'username': 'platform', 'password': PASSWORD}, **HTTP_IP)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'COOKIE_SECURE=auto', status_code=403)
        # No banner where the cookies do work.
        self.assertNotContains(self.client.get(reverse('login'), secure=True, HTTP_HOST='vote.example.com'),
                               "Sign-in can't work")
        self.assertNotContains(self.client.get(reverse('login'), HTTP_HOST='localhost:8000'), "Sign-in can't work")

    @override_settings(COOKIE_SECURE_MODE='never', COOKIE_SECURE=False, SESSION_COOKIE_SECURE=False,
                       CSRF_COOKIE_SECURE=False)
    def test_never_mode_is_never_secure(self):
        page = self.client.get(reverse('login'), secure=True, HTTP_HOST='vote.example.com')
        self.assertFalse(secure_flag(page, 'fv_csrftoken'))

    def test_cookie_secure_values(self):
        from vote_fund.settings import cookie_secure_mode

        for raw, mode in (('', 'auto'), (None, 'auto'), ('AUTO', 'auto'), ('True', 'always'), ('1', 'always'),
                          ('always', 'always'), ('False', 'never'), ('0', 'never'), (' off ', 'never')):
            self.assertEqual(cookie_secure_mode(raw), mode, raw)
        with self.assertRaises(ImproperlyConfigured):
            cookie_secure_mode('maybe')

    def test_expired_form_gets_a_helpful_page_not_a_bare_403(self):
        response = Client(enforce_csrf_checks=True).post(reverse('login'), {'username': 'x', 'password': 'y'},
                                                         **HTTP_IP)
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, 'This form could not be accepted', status_code=403)
        self.assertContains(response, 'href="/login/"', status_code=403)  # "Try again"
        self.assertNotContains(response, 'COOKIE_SECURE', status_code=403)


class StepUpTests(TestCase):
    """The emailed step-up code for unusual sign-ins."""

    def setUp(self):
        self.admin = make_user('platform', staff=True, superuser=True)
        KnownDevice.objects.create(user=self.admin, device_hash='another-browser')  # so this one is new

    def login(self, client=None):
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.client).post(reverse('login'), {'username': 'platform', 'password': PASSWORD})

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.console.EmailBackend')
    def test_no_email_delivery_means_no_code_the_admin_could_never_receive(self):
        response = self.login()
        self.assertRedirects(response, reverse('dashboard'), fetch_redirect_response=False)
        self.assertIn('_auth_user_id', self.client.session)
        skipped = AuditEvent.objects.get(event_type='AUTH_STEPUP_SKIPPED')
        self.assertEqual(skipped.metadata['reasons'], ['new device'])

    def test_with_email_delivery_the_code_is_still_required(self):
        self.assertRedirects(self.login(), reverse('account:mfa'))
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertContains(self.client.get(reverse('account:mfa')), 'Back to sign in')

    def test_trying_again_does_not_skip_the_code(self):
        self.assertRedirects(self.login(), reverse('account:mfa'))
        code = re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)
        # A second attempt inside the resend cooldown (or by someone else holding the
        # password) used to sign in on the password alone.
        other = Client()
        self.assertRedirects(self.login(other), reverse('account:mfa'))
        self.assertNotIn('_auth_user_id', other.session)
        self.assertEqual(OTPChallenge.objects.count(), 1)  # the code already in the inbox
        with self.captureOnCommitCallbacks(execute=True):
            other.post(reverse('account:mfa'), {'method': 'email', 'code': code})
        self.assertIn('_auth_user_id', other.session)

    def test_no_usable_code_and_none_can_be_sent_refuses_politely(self):
        self.assertRedirects(self.login(), reverse('account:mfa'))
        OTPChallenge.objects.update(consumed_at=timezone.now())  # used up, and still inside the cooldown
        response = self.login(Client())
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, 'Too many sign-in codes', status_code=429)


@patch.dict(os.environ, {'DJANGO_SUPERUSER_USERNAME': 'seeded', 'DJANGO_SUPERUSER_EMAIL': 'seeded@example.com',
                         'DJANGO_SUPERUSER_PASSWORD': 'Seeded-admin-pass-26'})
class SeedAdminRestartTests(TestCase):
    def seed(self):
        out = io.StringIO()
        call_command('seed_admin', stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def test_restart_keeps_admins_signed_in_and_unlocks_them(self):
        self.seed()
        self.assertTrue(self.client.login(username='seeded', password='Seeded-admin-pass-26'))
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)
        user = User.objects.get(username='seeded')
        UserSecurity.objects.update_or_create(user=user, defaults={
            'failed_login_count': 9, 'locked_until': timezone.now() + timedelta(hours=4)})
        hash_before = user.password
        output = self.seed()  # what every web container start runs
        user.refresh_from_db()
        self.assertEqual(user.password, hash_before)
        self.assertEqual(self.client.get(reverse('dashboard')).status_code, 200)  # session survived
        self.assertIn('Unlocked "seeded"', output)
        security = UserSecurity.objects.get(user=user)
        self.assertEqual((security.failed_login_count, security.locked_until), (0, None))
        # A rotated password still takes effect.
        with patch.dict(os.environ, {'DJANGO_SUPERUSER_PASSWORD': 'Rotated-admin-pass-27'}):
            self.seed()
        user.refresh_from_db()
        self.assertTrue(user.check_password('Rotated-admin-pass-27'))


class ConsoleErrorHeaderTests(TestCase):
    """The errors the browser console showed on http://16.192.12.135:5000/login/."""

    def test_permissions_policy_has_only_recognised_features(self):
        policy = self.client.get(reverse('login'), **HTTP_IP)['Permissions-Policy']
        self.assertNotIn('interest-cohort', policy)
        self.assertIn('camera=(self)', policy)

    def test_coop_only_where_browsers_honour_it(self):
        self.assertNotIn('Cross-Origin-Opener-Policy', self.client.get(reverse('login'), **HTTP_IP))
        https = self.client.get(reverse('login'), secure=True, HTTP_HOST='vote.example.com')
        self.assertEqual(https['Cross-Origin-Opener-Policy'], 'same-origin')
        local = self.client.get(reverse('login'), HTTP_HOST='localhost:8000')
        self.assertEqual(local['Cross-Origin-Opener-Policy'], 'same-origin')

    def test_cdn_source_maps_are_allowed_by_csp(self):
        csp = self.client.get(reverse('login'), **HTTP_IP)['Content-Security-Policy']
        connect = csp.split('connect-src')[1].split(';')[0]
        for host in ('https://cdn.jsdelivr.net', 'https://cdnjs.cloudflare.com'):
            self.assertIn(host, connect)

    def test_favicon(self):
        response = self.client.get('/favicon.ico')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].endswith('favicon.svg'))
        self.assertContains(self.client.get(reverse('login')), 'rel="icon"')
