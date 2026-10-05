import re
from unittest import mock

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.urls import reverse

from core import http, idempotency, ratelimit, rbac
from core.tests.factories import PASSWORD, grant, make_event, make_org, make_user


class RBACTests(TestCase):
    def setUp(self):
        self.admin_a = make_user('admin_a')
        self.org_a = make_org('University A', admin=self.admin_a)
        self.admin_b = make_user('admin_b')
        self.org_b = make_org('Company B', admin=self.admin_b)
        self.event_a = make_event(org=self.org_a, title='A election')
        self.event_b = make_event(org=self.org_b, title='B election')

    def test_org_admin_scoped_to_own_tenant(self):
        self.assertTrue(rbac.has_perm(self.admin_a, 'election.edit', self.event_a))
        self.assertFalse(rbac.has_perm(self.admin_a, 'election.edit', self.event_b))
        self.assertEqual(list(rbac.events_for_user(self.admin_a)), [self.event_a])
        with self.assertRaises(PermissionDenied):
            rbac.check_perm(self.admin_a, 'voter.view', self.event_b)

    def test_event_scoped_role(self):
        officer = make_user('officer')
        grant(officer, 'ELECTION_OFFICER', self.org_a, event=self.event_a)
        other_event = make_event(org=self.org_a, title='Other A')
        self.assertTrue(rbac.has_perm(officer, 'voter.credentials', self.event_a))
        self.assertFalse(rbac.has_perm(officer, 'voter.credentials', other_event))
        self.assertFalse(rbac.has_perm(officer, 'results.certify', self.event_a))

    def test_auditor_is_read_only(self):
        auditor = make_user('auditor')
        grant(auditor, 'ELECTION_AUDITOR', self.org_a)
        perms = rbac.user_permissions(auditor, self.event_a)
        self.assertTrue({'audit.view', 'results.recount', 'vote.view'} <= perms)
        self.assertFalse(perms & {'election.edit', 'voter.edit', 'results.certify', 'refund.create', 'candidate.edit'})

    def test_platform_admin_and_unknown_permission(self):
        staff = make_user('staff', staff=True)
        self.assertTrue(rbac.has_perm(staff, 'platform.admin'))
        with self.assertRaises(ValueError):
            rbac.has_perm(staff, 'made.up')

    def test_roles_are_synced_by_migrate(self):
        from core.models import Role

        self.assertEqual(set(Role.objects.values_list('code', flat=True)), set(rbac.ROLE_DEFINITIONS))


class SecurityHeaderTests(TestCase):
    def test_csp_nonce_and_headers(self):
        response = self.client.get(reverse('home'))
        csp = response['Content-Security-Policy']
        nonce = re.search(r"'nonce-([^']+)'", csp).group(1)
        self.assertIn(f'nonce="{nonce}"', response.content.decode())
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertNotIn("'unsafe-inline'", csp.split('script-src')[1].split(';')[0])
        self.assertEqual(response['X-Frame-Options'], 'DENY')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('Permissions-Policy', response)
        self.assertTrue(response['X-Request-ID'])

    def test_correlation_id_is_echoed_when_valid(self):
        response = self.client.get(reverse('home'), HTTP_X_REQUEST_ID='abc-12345678')
        self.assertEqual(response['X-Request-ID'], 'abc-12345678')
        response = self.client.get(reverse('home'), HTTP_X_REQUEST_ID='<script>')
        self.assertNotEqual(response['X-Request-ID'], '<script>')

    def test_sensitive_pages_not_cached(self):
        make_user('cacheuser')
        self.client.login(username='cacheuser', password=PASSWORD)
        response = self.client.get(reverse('account:security'))
        self.assertIn('no-store', response['Cache-Control'])

    def test_health_endpoints(self):
        live = self.client.get('/healthz/live')
        self.assertEqual(live.json()['status'], 'alive')
        ready = self.client.get('/healthz/ready')
        self.assertEqual(ready.status_code, 200)
        self.assertNotIn('checks', ready.json())

    @override_settings(ALLOWED_HOSTS=['vote.example.com'], METRICS_TOKEN=None)
    def test_probes_work_with_load_balancer_host_headers(self):
        """ALB health checks send the target IP as Host and the image
        HEALTHCHECK sends 127.0.0.1; neither is an allowed host in production."""
        for host in ('10.0.1.23:8000', '127.0.0.1:8000'):
            self.assertEqual(self.client.get('/healthz/live', HTTP_HOST=host).status_code, 200)
            ready = self.client.get('/healthz/ready', HTTP_HOST=host, HTTP_AUTHORIZATION='Bearer None')
            self.assertEqual(ready.status_code, 200)
            self.assertNotIn('checks', ready.json())  # no token configured -> no details
        # Everything else still gets host validation.
        self.assertEqual(self.client.get('/', HTTP_HOST='10.0.1.23:8000').status_code, 400)
        self.assertEqual(self.client.get('/', HTTP_HOST='vote.example.com').status_code, 200)

    @override_settings(METRICS_TOKEN='metrics-secret')
    def test_metrics_require_token(self):
        self.assertEqual(self.client.get('/metrics').status_code, 403)
        response = self.client.get('/metrics', HTTP_AUTHORIZATION='Bearer metrics-secret')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'fv_http_requests_total', response.content)

    @override_settings(SECURITY_CONTACT='security@example.com')
    def test_well_known_endpoints(self):
        import re
        from datetime import datetime, timezone as dt_timezone

        body = self.client.get('/.well-known/security.txt').content.decode()
        self.assertIn('Contact: mailto:security@example.com', body)
        expires = datetime.strptime(re.search(r'Expires: (\S+)', body).group(1), '%Y-%m-%dT%H:%M:%S.%fZ') \
            .replace(tzinfo=dt_timezone.utc)
        days_ahead = (expires - datetime.now(dt_timezone.utc)).days
        self.assertTrue(0 < days_ahead < 365, 'RFC 9116: Expires must be in the future and under a year away')
        key = self.client.get('/.well-known/flexyvotes-signing-key.json').json()
        self.assertEqual(key['algorithm'], 'Ed25519')

    @override_settings(API_CORS_ALLOWED_ORIGINS=['https://app.example.com'])
    def test_cors_only_for_allowed_origins_on_api(self):
        ok = self.client.options('/api/v1/elections', HTTP_ORIGIN='https://app.example.com',
                                 HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET')
        self.assertEqual(ok['Access-Control-Allow-Origin'], 'https://app.example.com')
        bad = self.client.options('/api/v1/elections', HTTP_ORIGIN='https://evil.example.com',
                                  HTTP_ACCESS_CONTROL_REQUEST_METHOD='GET')
        self.assertNotIn('Access-Control-Allow-Origin', bad)

    def test_logout_requires_post(self):
        make_user('someone')
        self.client.login(username='someone', password=PASSWORD)
        self.assertEqual(self.client.get(reverse('logout')).status_code, 405)

    def test_open_redirect_is_blocked_on_login(self):
        make_user('victim')
        response = self.client.post(reverse('login') + '?next=https://evil.example.com/',
                                    {'username': 'victim', 'password': PASSWORD})
        self.assertFalse(response['Location'].startswith('https://evil'))


class SsrfAndUtilityTests(TestCase):
    def test_outbound_allow_list(self):
        self.assertEqual(http.validate_url('https://api.paystack.co/x'), 'api.paystack.co')
        for url in ('http://api.paystack.co/x', 'https://169.254.169.254/latest', 'https://evil.example.com/'):
            with self.assertRaises(http.OutboundRequestBlocked):
                http.validate_url(url)

    def test_dynamic_hosts_must_resolve_publicly(self):
        with mock.patch('core.http.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('10.0.0.5', 443))]):
            with self.assertRaises(http.OutboundRequestBlocked):
                http.validate_url('https://idp.internal.example/', extra_hosts=['idp.internal.example'])
        with mock.patch('core.http.socket.getaddrinfo', return_value=[(2, 1, 6, '', ('93.184.216.34', 443))]):
            self.assertEqual(http.validate_url('https://idp.example.org/', extra_hosts=['idp.example.org']), 'idp.example.org')

    def test_circuit_breaker_opens_after_failures(self):
        breaker = http.CircuitBreaker('test-breaker', failure_threshold=3)
        for _ in range(3):
            breaker.record_failure()
        self.assertTrue(breaker.is_open())

    def test_rate_limit_window(self):
        results = [ratelimit.hit('t', 'ip', 3, 60)[0] for _ in range(5)]
        self.assertEqual(results, [True, True, True, False, False])

    def test_idempotency_records(self):
        record, replay = idempotency.begin('scope', 'key-abcdefgh', {'a': 1})
        self.assertFalse(replay)
        with self.assertRaises(idempotency.IdempotencyInProgress):
            idempotency.begin('scope', 'key-abcdefgh', {'a': 1})
        idempotency.complete(record, 201, {'ok': True})
        again, replay = idempotency.begin('scope', 'key-abcdefgh', {'a': 1})
        self.assertTrue(replay)
        self.assertEqual(again.response_body, {'ok': True})
        with self.assertRaises(idempotency.IdempotencyConflict):
            idempotency.begin('scope', 'key-abcdefgh', {'a': 2})

    def test_client_ip_honours_trusted_proxies_only(self):
        from django.test import RequestFactory

        from core.utils import client_ip

        request = RequestFactory().get('/', REMOTE_ADDR='10.0.0.1', HTTP_X_FORWARDED_FOR='1.2.3.4, 5.6.7.8')
        self.assertEqual(client_ip(request), '10.0.0.1')
        with override_settings(TRUSTED_PROXY_COUNT=1):
            self.assertEqual(client_ip(request), '5.6.7.8')

    def test_csv_formula_injection_neutralised(self):
        from voting.views import sanitize_csv_value

        self.assertEqual(sanitize_csv_value('=HYPERLINK("x")'), '\'=HYPERLINK("x")')
        self.assertEqual(sanitize_csv_value('normal'), 'normal')


class UploadAndOtpHardeningTests(TestCase):
    def test_document_upload_rejects_spoofed_content(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from core.storage import validate_document

        self.assertIsNone(validate_document(SimpleUploadedFile('manifesto.pdf', b'%PDF-1.7 real pdf')))
        spoofed = SimpleUploadedFile('manifesto.pdf', b'MZ\x90\x00 windows executable')
        self.assertEqual(validate_document(spoofed), 'File content does not match its extension.')
        self.assertIn('Unsupported file type', validate_document(SimpleUploadedFile('run.exe', b'MZ\x90\x00')))
        self.assertIn('too large', validate_document(SimpleUploadedFile('big.pdf', b'%PDF' + b'0' * 64), max_bytes=32))

    def test_otp_brute_force_burns_the_challenge(self):
        from core import otp
        from core.models import OTPChallenge

        with self.captureOnCommitCallbacks(execute=True):
            challenge = otp.issue('login', 'user', '42', OTPChallenge.Channel.EMAIL, 'someone@example.com')
        for _ in range(challenge.max_attempts):
            with self.assertRaises(otp.OTPError):
                otp.verify(challenge.pk, '000000', purpose='login', subject_type='user', subject_id='42')
        # Even the right code is refused once the attempts are used up.
        from django.core import mail

        code = re.search(r'\b(\d{6})\b', mail.outbox[-1].body).group(1)
        with self.assertRaisesMessage(otp.OTPError, 'Too many incorrect attempts'):
            otp.verify(challenge.pk, code, purpose='login', subject_type='user', subject_id='42')


class DeployCheckTests(TestCase):
    def test_half_configured_cloudinary_is_reported(self):
        from core.checks import platform_security_checks

        with override_settings(MEDIA_STORAGE_MISCONFIGURED=True):
            self.assertIn('flexyvotes.W007', [i.id for i in platform_security_checks(None)])
        with override_settings(MEDIA_STORAGE_MISCONFIGURED=False):
            self.assertNotIn('flexyvotes.W007', [i.id for i in platform_security_checks(None)])
