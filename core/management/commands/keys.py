"""Key management.

  generate-kek            print a new 32-byte key-encryption key (for FIELD_ENCRYPTION_KEYS)
  generate-signing-key    print a new Ed25519 signing key pair (SIGNING_PRIVATE_KEY)
  generate-blind-index    print a new BLIND_INDEX_KEY
  rotate                  create a new data key and re-encrypt every encrypted column with it
  rewrap                  re-wrap every data key with the current (first) KEK - run after adding a new KEK
  backup --out FILE       passphrase-encrypted backup of the KEKs and wrapped data keys
  restore-check --in FILE verify a backup can be decrypted (does not change anything)
  reindex                 recompute blind indexes (after changing BLIND_INDEX_KEY)
  status                  show data keys and the active KEK
"""
import base64
import getpass
import json
import os

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core import audit, crypto
from core.fields import EncryptedTextField


def encrypted_columns():
    for model in apps.get_models():
        fields = [f for f in model._meta.get_fields() if isinstance(f, EncryptedTextField)]
        if fields:
            yield model, fields


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument('action', choices=['generate-kek', 'generate-signing-key', 'generate-blind-index', 'rotate',
                                               'rewrap', 'backup', 'restore-check', 'reindex', 'status'])
        parser.add_argument('--out')
        parser.add_argument('--in', dest='infile')
        parser.add_argument('--passphrase-env', default='KEY_BACKUP_PASSPHRASE',
                            help='Environment variable holding the backup passphrase (prompted if unset).')

    def handle(self, *args, **options):
        getattr(self, 'do_' + options['action'].replace('-', '_'))(options)

    def do_generate_kek(self, options):
        kid = 'k' + crypto.random_token(4).replace('-', '').replace('_', '')[:6]
        self.stdout.write(f'{kid}:{base64.urlsafe_b64encode(os.urandom(32)).decode()}')

    def do_generate_signing_key(self, options):
        private, public = crypto.generate_signing_keypair()
        self.stdout.write(f'SIGNING_PRIVATE_KEY={private}\n# public key (publish): {public}')

    def do_generate_blind_index(self, options):
        self.stdout.write(base64.urlsafe_b64encode(os.urandom(32)).decode())

    def do_status(self, options):
        from core.models import DataKey

        provider = crypto.key_provider()
        self.stdout.write(f'KEK provider: {provider.name}, active KEK: {provider.active_id}')
        stale = 0
        for key in DataKey.objects.all():
            self.stdout.write(f'  data key {key.pk} purpose={key.purpose} provider={key.provider} kek={key.kek_id} '
                              f'active={key.is_active}')
            stale += key.provider != provider.name or key.kek_id != provider.active_id
        if stale:
            self.stdout.write(self.style.WARNING(
                f'{stale} data key(s) are not wrapped by the active KEK. Run `manage.py keys rewrap`.'))
        source = 'SIGNING_PRIVATE_KEY' if settings.SIGNING_PRIVATE_KEY else 'derived from SECRET_KEY'
        public = crypto.public_key_b64()
        self.stdout.write(f'Signing key ({source}): public {public} fingerprint {crypto.key_fingerprint(public)}')
        self.stdout.write(f'Trusted previous public keys: {len(settings.SIGNING_PREVIOUS_PUBLIC_KEYS)}')
        self.stdout.write(f'Blind index key: {"BLIND_INDEX_KEY" if settings.BLIND_INDEX_KEY else "derived from SECRET_KEY"}')

    def do_rewrap(self, options):
        from core.models import DataKey

        provider = crypto.key_provider()
        count = 0
        with transaction.atomic():
            for key in DataKey.objects.select_for_update():
                dek = crypto.unwrap_data_key(key)
                key.kek_id, key.wrapped_key = provider.wrap(dek)
                key.provider = provider.name
                key.save(update_fields=['kek_id', 'wrapped_key', 'provider'])
                count += 1
        crypto.reset_key_caches()
        audit.record('KEYS_REWRAPPED', summary=f'{count} data keys re-wrapped with KEK {provider.active_id}')
        self.stdout.write(self.style.SUCCESS(f'Re-wrapped {count} data keys with {provider.active_id}.'))

    def do_rotate(self, options):
        new_key = crypto.create_data_key()
        crypto._active_dek.clear()
        total = 0
        for model, fields in encrypted_columns():
            names = [f.name for f in fields]
            manager = model._base_manager
            for instance in manager.all().iterator():
                values = {name: getattr(instance, name) for name in names}
                updates = {}
                for field in fields:
                    value = values[field.name]
                    if value in (None, ''):
                        continue
                    updates[field.attname] = field.get_prep_value(value)
                if updates:
                    # Queryset .update() bypasses append-only save() guards, which
                    # is fine: only ciphertext changes, never plaintext.
                    manager.filter(pk=instance.pk).update(**updates)
                    total += 1
        audit.record('KEYS_ROTATED', summary=f'Data key rotated to {new_key.pk}; {total} rows re-encrypted')
        self.stdout.write(self.style.SUCCESS(f'Rotated to data key {new_key.pk}; re-encrypted {total} rows.'))

    def do_reindex(self, options):
        from elections.models import Voter
        from payments.models import Payment

        count = 0
        for voter in Voter.objects.all().iterator():
            voter.set_email(voter.email)
            voter.phone_index = crypto.blind_index(voter.phone, 'phone') if voter.phone else ''
            Voter.objects.filter(pk=voter.pk).update(email_index=voter.email_index, phone_index=voter.phone_index)
            count += 1
        for payment in Payment.objects.all().iterator():
            Payment.objects.filter(pk=payment.pk).update(
                payer_email_index=crypto.blind_index(payment.payer_email, 'email') if payment.payer_email else '',
                payer_phone_index=crypto.blind_index(payment.payer_phone, 'phone') if payment.payer_phone else '')
            count += 1
        audit.record('KEYS_REINDEXED', summary=f'{count} blind indexes recomputed')
        self.stdout.write(self.style.SUCCESS(f'Recomputed {count} blind indexes.'))

    def _passphrase(self, options, confirm=False):
        value = os.environ.get(options['passphrase_env'])
        if value:
            return value
        value = getpass.getpass('Backup passphrase: ')
        if confirm and value != getpass.getpass('Repeat passphrase: '):
            raise CommandError('Passphrases do not match.')
        if len(value) < 12:
            raise CommandError('Use a passphrase of at least 12 characters.')
        return value

    def do_backup(self, options):
        from core.models import DataKey

        if not options['out']:
            raise CommandError('--out is required')
        payload = {
            'format': 'flexyvotes-key-backup-v1',
            'field_encryption_keys': settings.FIELD_ENCRYPTION_KEYS or '(derived from SECRET_KEY)',
            'kms_key_id': settings.KMS_KEY_ID or '',
            'signing_private_key': settings.SIGNING_PRIVATE_KEY or '(derived from SECRET_KEY)',
            'blind_index_key': settings.BLIND_INDEX_KEY or '(derived from SECRET_KEY)',
            'data_keys': [{'id': k.pk, 'purpose': k.purpose, 'kek_id': k.kek_id, 'wrapped_key': k.wrapped_key,
                           'active': k.is_active} for k in DataKey.objects.all()],
        }
        blob = crypto.encrypt_with_passphrase(json.dumps(payload).encode(), self._passphrase(options, confirm=True))
        with open(options['out'], 'w', encoding='utf-8') as handle:
            handle.write(blob)
        audit.record('KEYS_BACKED_UP', summary='Encrypted key backup written')
        self.stdout.write(self.style.SUCCESS(f'Encrypted key backup written to {options["out"]}. Store it offline.'))

    def do_restore_check(self, options):
        if not options['infile']:
            raise CommandError('--in is required')
        with open(options['infile'], encoding='utf-8') as handle:
            blob = handle.read()
        data = json.loads(crypto.decrypt_with_passphrase(blob, self._passphrase(options)))
        self.stdout.write(self.style.SUCCESS(f'Backup OK: {len(data["data_keys"])} data keys, format {data["format"]}.'))
