import io
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from core import crypto
from core.tests.factories import (add_position, add_voters, grant, institutional_setup, make_event, make_org, make_user,
                                  open_institutional)
from elections import integrity, keys, lifecycle, results, voters as voter_service
from elections.casting import cast_ballot, issue_authorization
from elections.eligibility import ballot_style, election_eligibility
from elections.models import ApprovalRequest, Constituency, EligibilityRule, EvidenceItem, TrusteeShare, Voter
from voting.models import Event


class ApprovalAndFreezeTests(TestCase):
    def setUp(self):
        self.s = institutional_setup(dual=True)
        self.event = open_institutional(self.s['event'], self.s['admin'], self.s['reviewer'])
        self.second = make_user('second_admin')
        grant(self.second, 'ORG_ADMIN', self.s['org'])

    def test_extension_requires_a_different_approver(self):
        new_end = (self.event.end_date + timedelta(hours=3)).isoformat()
        req = integrity.request_approval(self.event, ApprovalRequest.Action.EXTEND_VOTING, {'new_end': new_end},
                                         'Network outage', self.s['admin'])
        self.assertEqual(req.status, ApprovalRequest.Status.PENDING)
        with self.assertRaises(integrity.IntegrityControlError):
            integrity.decide(req, self.s['admin'], True)
        outsider = make_user('outsider')
        with self.assertRaises(PermissionDenied):
            integrity.decide(req, outsider, True)
        integrity.decide(req, self.second, True, 'Agreed')
        req.refresh_from_db()
        self.assertEqual(req.status, ApprovalRequest.Status.EXECUTED)
        self.event.refresh_from_db()
        self.assertEqual(self.event.end_date.isoformat(), new_end)

    def test_reason_is_mandatory(self):
        with self.assertRaises(integrity.IntegrityControlError):
            integrity.request_approval(self.event, ApprovalRequest.Action.UNFREEZE, {'scope': 'ballot'}, ' ', self.s['admin'])

    def test_unfreeze_needs_approval_while_open(self):
        req = integrity.unfreeze(self.event, 'ballot', self.s['admin'], 'Typo in a candidate name')
        self.assertEqual(req.status, ApprovalRequest.Status.PENDING)
        self.assertTrue(Event.objects.get(pk=self.event.pk).ballot_frozen)
        integrity.decide(req, self.second, True)
        self.assertFalse(Event.objects.get(pk=self.event.pk).ballot_frozen)

    def test_rejection_and_expiry(self):
        req = integrity.request_approval(self.event, ApprovalRequest.Action.UNFREEZE, {'scope': 'voters'}, 'x', self.s['admin'])
        integrity.decide(req, self.second, False, 'No')
        self.assertEqual(req.status, ApprovalRequest.Status.REJECTED)
        req2 = integrity.request_approval(self.event, ApprovalRequest.Action.UNFREEZE, {'scope': 'voters'}, 'y', self.s['admin'])
        ApprovalRequest.objects.filter(pk=req2.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        req2.refresh_from_db()
        with self.assertRaises(integrity.IntegrityControlError):
            integrity.decide(req2, self.second, True)

    def test_edit_policy_matrix(self):
        self.assertFalse(lifecycle.edit_policy(self.event, 'ballot')[0])
        self.assertFalse(lifecycle.edit_policy(self.event, 'config')[0])
        draft = make_event(org=self.s['org'], title='Draft')
        self.assertEqual(lifecycle.edit_policy(draft, 'ballot'), (True, False, ''))
        approved = make_event(org=self.s['org'], title='Approved', status=Event.Status.APPROVED)
        self.assertTrue(lifecycle.edit_policy(approved, 'ballot')[1])  # editing resets approval
        lifecycle.require_editable(approved, 'ballot', self.s['admin'])
        self.assertEqual(Event.objects.get(pk=approved.pk).status, Event.Status.DRAFT)

    def test_legal_hold_blocks_archive_and_release_needs_approval(self):
        integrity.set_legal_hold(self.event, self.s['admin'], True, 'Court order')
        lifecycle.transition(self.event, 'close', actor=self.s['admin'])
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.PUBLISHED)
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.transition(Event.objects.get(pk=self.event.pk), 'archive', actor=self.s['admin'])
        req = integrity.set_legal_hold(Event.objects.get(pk=self.event.pk), self.s['admin'], False, 'Case closed')
        self.assertEqual(req.status, ApprovalRequest.Status.PENDING)

    def test_disputes_incidents_and_evidence(self):
        dispute = integrity.file_dispute(self.event, filer_name='Ama', filer_email='ama@x.com', filer_role='VOTER',
                                         category='CONDUCT', subject='Issue', description='Details')
        integrity.update_dispute(dispute, self.s['admin'], 'UNDER_REVIEW')
        upload = SimpleUploadedFile('photo.png', b'\x89PNG\r\n\x1a\n' + b'0' * 100, content_type='image/png')
        item = integrity.preserve_evidence(self.event, upload, 'Queue photo', self.s['admin'], dispute=dispute)
        self.assertTrue(integrity.evidence_intact(item))
        with self.assertRaises(PermissionDenied):
            item.title = 'changed'
            item.save()
        with self.assertRaises(PermissionDenied):
            EvidenceItem.objects.all().delete()
        incident = integrity.open_incident(title='DDoS', description='Traffic spike', severity='HIGH', actor=self.s['admin'],
                                           event=self.event)
        integrity.update_incident(incident, self.s['admin'], 'RESOLVED', 'Mitigated by CDN')
        incident.refresh_from_db()
        self.assertIsNotNone(incident.resolved_at)
        self.assertEqual(incident.notes.count(), 1)

    def test_reopen_after_close_with_approval(self):
        lifecycle.transition(self.event, 'close', actor=self.s['admin'])
        new_end = (timezone.now() + timedelta(hours=2)).isoformat()
        req = integrity.request_approval(Event.objects.get(pk=self.event.pk), ApprovalRequest.Action.REOPEN_VOTING,
                                         {'new_end': new_end}, 'Power cut', self.s['admin'])
        integrity.decide(req, self.second, True)
        self.assertEqual(Event.objects.get(pk=self.event.pk).status, Event.Status.OPEN)


@override_settings()
class TrusteeCustodyTests(TestCase):
    def test_k_of_n_trustees_required_to_tally(self):
        s = institutional_setup(dual=False)
        event = s['event']
        Event.objects.filter(pk=event.pk).update(key_custody=Event.KeyCustody.TRUSTEES, trustee_threshold=2)
        event.refresh_from_db()
        trustees = [make_user(f'trustee{i}') for i in range(3)]
        for t in trustees:
            keys.add_trustee(event, t, s['admin'])
        event = open_institutional(event, s['admin'], s['reviewer'])
        self.assertIsNone(event.ballot_key.wrapped_private_key)
        shares = [keys.collect_share(event, t) for t in trustees]
        with self.assertRaises(keys.KeyCustodyError):
            keys.collect_share(event, trustees[0])
        token, _ = issue_authorization(event, s['voters'][0], 'CODE')
        cast_ballot(event, token, {str(s['president'].pk): [s['president_candidates'][0].pk], str(s['senate'].pk): []})
        lifecycle.transition(event, 'close', actor=s['admin'])
        lifecycle.transition(event, 'start_tally', actor=s['admin'])
        with self.assertRaises(keys.KeyCustodyError):
            results.run_tally(Event.objects.get(pk=event.pk), actor=s['admin'])
        with self.assertRaises(keys.KeyCustodyError):
            keys.submit_share(event, trustees[0], shares[1])
        keys.submit_share(event, trustees[0], shares[0])
        keys.submit_share(event, trustees[2], shares[2])
        result = results.run_tally(Event.objects.get(pk=event.pk), actor=s['admin'])
        self.assertEqual(result.ballots_counted, 1)
        self.assertFalse(TrusteeShare.objects.exclude(submitted_share=None).exists())


class VoterRollTests(TestCase):
    def setUp(self):
        self.admin = make_user('roll_admin')
        self.org = make_org('Roll Uni', admin=self.admin)
        root = Constituency.objects.create(organization=self.org, name='University', code='UNI', kind='ROOT')
        self.science = Constituency.objects.create(organization=self.org, parent=root, name='Science', code='SCI', kind='FACULTY')
        self.physics = Constituency.objects.create(organization=self.org, parent=self.science, name='Physics', code='PHY',
                                                   kind='DEPARTMENT')
        self.arts = Constituency.objects.create(organization=self.org, parent=root, name='Arts', code='ART', kind='FACULTY')
        self.event = make_event(org=self.org, organizer=self.admin)

    def test_constituency_tree_paths(self):
        self.assertTrue(self.physics.is_within(self.science))
        self.assertFalse(self.physics.is_within(self.arts))
        self.assertEqual(self.physics.depth, 2)

    def test_csv_import_with_constituencies_attributes_and_errors(self):
        content = ('student_id,name,email,phone,department,level\n'
                   'S1,Ama,ama@uni.edu,0241234567,PHY,400\n'
                   'S2,Kofi,kofi@uni.edu,,ART,100\n'
                   'S3,Bad,not-an-email,,PHY,200\n'
                   'S4,Who,who@uni.edu,,NOPE,200\n'
                   ',,,,,\n')
        rows = voter_service.parse_upload(SimpleUploadedFile('roll.csv', content.encode()))
        report = voter_service.import_voters(self.event, rows, self.admin)
        self.assertEqual((report.created, len(report.errors)), (2, 2))
        ama = Voter.objects.get(identifier='S1')
        self.assertEqual((ama.constituency, ama.attributes['level'], ama.phone), (self.physics, '400', '+233241234567'))
        self.assertTrue(ama.credential_hash and ama.credential_ciphertext)
        again = voter_service.import_voters(self.event, rows[:1], self.admin)
        self.assertEqual((again.created, again.updated), (0, 1))

    def test_legacy_headerless_csv(self):
        rows = voter_service.parse_upload(SimpleUploadedFile('ids.csv', b'ID100,a@uni.edu\nID101\n'))
        report = voter_service.import_voters(self.event, rows, self.admin)
        self.assertEqual(report.created, 2)

    def test_xlsx_import(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(['identifier', 'full_name', 'email', 'constituency'])
        sheet.append(['X1', 'Excel Voter', 'x1@uni.edu', 'SCI'])
        buffer = io.BytesIO()
        workbook.save(buffer)
        rows = voter_service.parse_upload(SimpleUploadedFile('roll.xlsx', buffer.getvalue()))
        report = voter_service.import_voters(self.event, rows, self.admin, source='XLSX')
        self.assertEqual(report.created, 1)
        self.assertEqual(Voter.objects.get(identifier='X1').constituency, self.science)

    def test_eligibility_rules_and_ballot_styles(self):
        president, _ = add_position(self.event, 'President')
        physics_rep, _ = add_position(self.event, 'Physics Rep', constituency=self.physics)
        final_year, _ = add_position(self.event, 'Final-year Rep')
        EligibilityRule.objects.create(election=self.event, position=final_year, kind='ATTRIBUTE_IN', attribute='level',
                                       values=['400'])
        rows = [{'identifier': 'P1', 'constituency': 'PHY', 'attributes': {'level': '400'}},
                {'identifier': 'A1', 'constituency': 'ART', 'attributes': {'level': '100'}}]
        voter_service.import_voters(self.event, rows, self.admin)
        physicist, artist = Voter.objects.get(identifier='P1'), Voter.objects.get(identifier='A1')
        self.assertEqual(ballot_style(self.event, physicist), sorted([president.pk, physics_rep.pk, final_year.pk]))
        self.assertEqual(ballot_style(self.event, artist), [president.pk])
        EligibilityRule.objects.create(election=self.event, kind='EMAIL_DOMAIN', values=['uni.edu'],
                                       description='University email required')
        self.event._fv_rules = None
        eligible, reasons = election_eligibility(self.event, artist)
        self.assertFalse(eligible)
        self.assertIn('University email required', reasons)

    def test_credential_reset_export_and_suspension(self):
        voters, codes = add_voters(self.event, 2)
        new_code = voter_service.reset_credential(voters[0], self.admin)
        self.assertNotEqual(new_code, codes[voters[0].pk])
        rows = voter_service.export_credentials(self.event, self.admin)
        self.assertIn(new_code, [r['code'] for r in rows])
        voter_service.set_status(voters[1], Voter.Status.SUSPENDED, self.admin, 'Fee arrears')
        self.assertEqual(Voter.objects.get(pk=voters[1].pk).status, Voter.Status.SUSPENDED)
        stats = voter_service.turnout(self.event)
        self.assertEqual((stats['total'], stats['eligible']), (2, 1))

    @override_settings()
    def test_plan_voter_limit_enforced(self):
        from billing.service import BillingLimitError

        rows = [{'identifier': f'L{i}', 'attributes': {}} for i in range(1001)]
        with self.assertRaises(BillingLimitError):
            voter_service.import_voters(self.event, rows, self.admin)

    def test_voter_list_frozen_after_open(self):
        add_position(self.event, 'President')
        add_voters(self.event, 1)
        open_institutional(self.event, self.admin)
        with self.assertRaises(lifecycle.LifecycleError):
            voter_service.import_voters(Event.objects.get(pk=self.event.pk), [{'identifier': 'LATE', 'attributes': {}}], self.admin)

    def test_anonymous_code_generation(self):
        created = voter_service.generate_anonymous_codes(self.event, 25, self.admin)
        self.assertEqual(created, 25)
        self.assertEqual(Voter.objects.filter(source='CODES').values('credential_hash').distinct().count(), 25)


class ConstituencyResultsTests(TestCase):
    def test_small_constituencies_are_suppressed(self):
        s = institutional_setup()
        org, event = s['org'], s['event']
        big = Constituency.objects.create(organization=org, name='Big', code='BIG')
        small = Constituency.objects.create(organization=org, name='Small', code='SMALL')
        Event.objects.filter(pk=event.pk).update(record_constituency_on_ballot=True, min_anonymity_set=2)
        voters, _ = add_voters(event, 3, prefix='CR')
        for voter, unit in zip(voters, (big, big, small)):
            voter.constituency = unit
            voter.save()
        event = open_institutional(Event.objects.get(pk=event.pk), s['admin'], s['reviewer'])
        for voter in voters:
            token, _ = issue_authorization(event, voter, 'CODE')
            cast_ballot(event, token, {str(s['president'].pk): [s['president_candidates'][0].pk], str(s['senate'].pk): []})
        lifecycle.transition(event, 'close', actor=s['admin'])
        result = results.run_tally(Event.objects.get(pk=event.pk), actor=s['admin'])
        by_name = {c['name']: c for c in result.data['constituencies']}
        self.assertTrue(by_name['Small']['suppressed'])
        self.assertIn('positions', by_name['Big'])
