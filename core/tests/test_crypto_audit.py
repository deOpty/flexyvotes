import io

from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import TestCase, override_settings

from core import audit, crypto
from core.models import AuditEvent, DataKey
from core.tests.factories import make_user


class CryptoTests(TestCase):
    def test_envelope_encryption_roundtrip_and_aad_binding(self):
        token = crypto.encrypt_str('secret value', aad='a.b.c')
        self.assertTrue(token.startswith('fv1$'))
        self.assertNotIn('secret', token)
        self.assertEqual(crypto.decrypt_str(token, aad='a.b.c'), 'secret value')
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_str(token, aad='x.y.z')

    def test_data_key_rotation_keeps_old_ciphertext_readable(self):
        old = crypto.encrypt_str('before rotation')
        crypto.create_data_key()
        new = crypto.encrypt_str('after rotation')
        self.assertNotEqual(crypto.data_key_id_of(old), crypto.data_key_id_of(new))
        self.assertEqual(crypto.decrypt_str(old), 'before rotation')
        self.assertEqual(DataKey.objects.filter(is_active=True).count(), 1)

    def test_kek_rotation(self):
        import base64
        import os

        key_a = base64.urlsafe_b64encode(os.urandom(32)).decode()
        key_b = base64.urlsafe_b64encode(os.urandom(32)).decode()
        with override_settings(FIELD_ENCRYPTION_KEYS=f'a:{key_a}'):
            crypto.reset_key_caches()
            token = crypto.encrypt_str('rotate me')
        with override_settings(FIELD_ENCRYPTION_KEYS=f'b:{key_b},a:{key_a}'):
            crypto.reset_key_caches()
            self.assertEqual(crypto.decrypt_str(token), 'rotate me')
            from django.core.management import call_command

            call_command('keys', 'rewrap')
            crypto.reset_key_caches()
        with override_settings(FIELD_ENCRYPTION_KEYS=f'b:{key_b}'):
            crypto.reset_key_caches()
            self.assertEqual(crypto.decrypt_str(token), 'rotate me')
        crypto.reset_key_caches()

    def test_upgrade_from_derived_kek_to_configured_kek(self):
        """Data written before FIELD_ENCRYPTION_KEYS was set must survive
        setting it, and `keys rewrap` must move every data key onto it."""
        import base64
        import os

        from django.core.management import call_command

        real = base64.urlsafe_b64encode(os.urandom(32)).decode()
        with override_settings(FIELD_ENCRYPTION_KEYS=None, KMS_KEY_ID=None):
            crypto.reset_key_caches()
            token = crypto.encrypt_str('written in development')
        self.assertEqual(DataKey.objects.get().kek_id, crypto.DEV_KEK_ID)
        with override_settings(FIELD_ENCRYPTION_KEYS=f'prod:{real}', KMS_KEY_ID=None):
            crypto.reset_key_caches()
            self.assertEqual(crypto.decrypt_str(token), 'written in development')
            self.assertEqual(crypto.key_provider().active_id, 'prod')
            status = io.StringIO()
            call_command('keys', 'status', stdout=status)
            self.assertIn('not wrapped by the active KEK', status.getvalue())
            call_command('keys', 'rewrap', stdout=io.StringIO())
            self.assertEqual(set(DataKey.objects.values_list('kek_id', flat=True)), {'prod'})
            crypto.reset_key_caches()
            self.assertEqual(crypto.decrypt_str(token), 'written in development')
            self.assertEqual(crypto.key_provider().wrap(os.urandom(32))[0], 'prod')
        crypto.reset_key_caches()

    def test_switch_from_local_kek_to_kms(self):
        import os
        from unittest import mock

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from django.core.management import call_command

        hsm_key = AESGCM.generate_key(bit_length=256)

        class FakeKms:
            name = 'aws-kms'

            def __init__(self, key_id, region):
                self.active_id = key_id

            def wrap(self, dek):
                nonce = os.urandom(12)
                return self.active_id, crypto.b64e(nonce + AESGCM(hsm_key).encrypt(nonce, dek, None))

            def unwrap(self, kek_id, wrapped):
                raw = crypto.b64d(wrapped)
                return AESGCM(hsm_key).decrypt(raw[:12], raw[12:], None)

        with override_settings(FIELD_ENCRYPTION_KEYS=None, KMS_KEY_ID=None):
            crypto.reset_key_caches()
            token = crypto.encrypt_str('before kms')
        with mock.patch.object(crypto, 'AwsKmsKeyProvider', FakeKms), \
                override_settings(FIELD_ENCRYPTION_KEYS=None, KMS_KEY_ID='arn:aws:kms:test'):
            crypto.reset_key_caches()
            self.assertEqual(crypto.decrypt_str(token), 'before kms')
            call_command('keys', 'rewrap', stdout=io.StringIO())
            self.assertEqual(set(DataKey.objects.values_list('provider', flat=True)), {'aws-kms'})
            crypto.reset_key_caches()
            self.assertEqual(crypto.decrypt_str(token), 'before kms')
        crypto.reset_key_caches()

    def test_verify_integrity_fails_when_keys_cannot_decrypt(self):
        """A restore with the wrong keys must fail verification immediately."""
        from django.core.management import call_command

        from core.tests.factories import make_event
        from elections.models import Voter

        event = make_event()
        Voter.objects.create(election=event, identifier='S1', full_name='Ama Mensah')
        out = io.StringIO()
        call_command('verify_integrity', '--skip-evidence', stdout=out)
        self.assertIn('decrypt elections.Voter.full_name', out.getvalue())
        with override_settings(SECRET_KEY='a-different-secret-key-as-after-a-bad-restore', FIELD_ENCRYPTION_KEYS=None,
                               KMS_KEY_ID=None):
            crypto.reset_key_caches()
            with self.assertRaises(SystemExit):
                call_command('verify_integrity', '--skip-evidence', stdout=io.StringIO(), stderr=io.StringIO())
        crypto.reset_key_caches()

    def test_encrypted_model_field_is_ciphertext_in_database(self):
        from core.tests.factories import make_event
        from elections.models import Voter

        voter = Voter(election=make_event(), identifier='X1', full_name='Ama Mensah')
        voter.set_email('ama@uni.edu')
        voter.save()
        with connection.cursor() as cursor:
            cursor.execute('SELECT full_name, email, email_index FROM elections_voter WHERE id = %s', [voter.pk])
            full_name, email, index = cursor.fetchone()
        self.assertTrue(full_name.startswith('fv1$') and email.startswith('fv1$'))
        self.assertEqual(index, crypto.blind_index('AMA@uni.edu ', 'email'))
        self.assertEqual(Voter.objects.get(pk=voter.pk).email, 'ama@uni.edu')

    def test_signatures(self):
        payload = crypto.canonical_json({'b': 1, 'a': [1, 2]})
        signature = crypto.sign(payload)
        public = crypto.public_key_b64()
        self.assertTrue(crypto.verify_signature(payload, signature, public))
        self.assertFalse(crypto.verify_signature(payload + b' ', signature, public))
        self.assertEqual(payload, b'{"a":[1,2],"b":1}')

    def test_ballot_sealing(self):
        private, public = crypto.generate_election_keypair()
        blob = crypto.seal(public, b'{"choice": 1}', aad=b'7:style')
        self.assertEqual(crypto.unseal(private, blob, aad=b'7:style'), b'{"choice": 1}')
        with self.assertRaises(crypto.CryptoError):
            crypto.unseal(private, blob, aad=b'8:style')

    def test_shamir_threshold(self):
        secret = b'\x00\x01' + b'k' * 30
        shares = crypto.split_secret(secret, 5, 3)
        self.assertEqual(crypto.combine_shares(shares[1:4]), secret)
        self.assertEqual(crypto.combine_shares([shares[0], shares[2], shares[4]]), secret)
        with self.assertRaises(crypto.CryptoError):
            crypto.combine_shares(shares[:2])
        with self.assertRaises(crypto.CryptoError):
            crypto.combine_shares([shares[0], shares[0], shares[1]])

    def test_merkle_proofs(self):
        leaves = [crypto.sha256_hex(str(i)) for i in range(7)]
        root = crypto.merkle_root(leaves)
        for index, leaf in enumerate(leaves):
            self.assertTrue(crypto.verify_merkle_proof(leaf, crypto.merkle_proof(leaves, index), root))
        self.assertFalse(crypto.verify_merkle_proof(crypto.sha256_hex('x'), crypto.merkle_proof(leaves, 0), root))

    def test_passphrase_backup(self):
        blob = crypto.encrypt_with_passphrase(b'keys', 'correct horse')
        self.assertEqual(crypto.decrypt_with_passphrase(blob, 'correct horse'), b'keys')
        with self.assertRaises(crypto.CryptoError):
            crypto.decrypt_with_passphrase(blob, 'wrong')


class AuditChainTests(TestCase):
    def test_chain_links_and_verifies(self):
        user = make_user('auditor')
        for i in range(5):
            audit.record('TEST_EVENT', actor=user, summary=f'event {i}', changes={'n': {'old': i, 'new': i + 1}})
        ok, count, _, _ = audit.verify_chain('platform')
        self.assertTrue(ok)
        self.assertEqual(count, 5)
        first, second = AuditEvent.objects.order_by('seq')[:2]
        self.assertEqual(second.prev_hash, first.hash)

    def test_orm_refuses_updates_and_deletes(self):
        entry = audit.record('TEST_EVENT', summary='x')
        with self.assertRaises(PermissionDenied):
            entry.summary = 'changed'
            entry.save()
        with self.assertRaises(PermissionDenied):
            entry.delete()
        with self.assertRaises(PermissionDenied):
            AuditEvent.objects.all().delete()
        with self.assertRaises(PermissionDenied):
            AuditEvent.objects.update(summary='y')

    def test_tampering_is_detected(self):
        for i in range(3):
            audit.record('TEST_EVENT', summary=f'event {i}')
        if connection.vendor == 'postgresql':
            from django.db import DatabaseError, transaction

            with self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("UPDATE core_auditevent SET summary = 'forged' WHERE seq = 2")
            return
        with connection.cursor() as cursor:
            cursor.execute("UPDATE core_auditevent SET summary = 'forged' WHERE seq = 2")
        ok, _, bad_seq, message = audit.verify_chain('platform')
        self.assertFalse(ok)
        self.assertEqual(bad_seq, 2)
        self.assertIn('modified', message)

    def test_deletion_is_detected(self):
        for i in range(3):
            audit.record('TEST_EVENT', summary=f'event {i}')
        if connection.vendor == 'postgresql':
            from django.db import DatabaseError, transaction

            with self.assertRaises(DatabaseError), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute('DELETE FROM core_auditevent WHERE seq = 3')
            return
        with connection.cursor() as cursor:
            cursor.execute('DELETE FROM core_auditevent WHERE seq = 3')
        ok, _, _, message = audit.verify_chain('platform')
        self.assertFalse(ok)
        self.assertIn('deleted', message)

    def test_database_triggers_make_tables_append_only(self):
        if connection.vendor != 'postgresql':
            self.skipTest('Append-only triggers exist only on PostgreSQL.')
        from django.db import DatabaseError, transaction

        tables = ['core_auditevent', 'elections_ballot', 'elections_evidenceitem', 'payments_paymentevent']
        with connection.cursor() as cursor:
            cursor.execute("SELECT event_object_table, event_manipulation FROM information_schema.triggers "
                           "WHERE trigger_name LIKE %s", ['%_append_only'])
            installed = set(cursor.fetchall())
        for table in tables:
            self.assertIn((table, 'UPDATE'), installed)
            self.assertIn((table, 'DELETE'), installed)
        audit.record('TEST_EVENT', summary='protected')
        for statement in ("UPDATE core_auditevent SET summary = 'x'", 'DELETE FROM core_auditevent'):
            with self.assertRaisesMessage(DatabaseError, 'append-only'), transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(statement)
        self.assertEqual(AuditEvent.objects.filter(summary='protected').count(), 1)

    def test_diff_helper(self):
        self.assertEqual(audit.diff({'a': 1, 'b': 2}, {'a': 1, 'b': 3}), {'b': {'old': 2, 'new': 3}})
