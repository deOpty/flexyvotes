"""Verify everything that is supposed to be tamper-evident.

Used after restoring a backup (see docs/DISASTER_RECOVERY.md) and on a
schedule: audit hash chains, signed configuration snapshots, result
certifications and their bulletin-board roots, evidence file hashes, and
that the configured keys can still unwrap every data key and decrypt the
encrypted columns (a restore with the wrong KEK must fail here, not later).
Exits non-zero if anything fails.
"""
import sys

from django.core.management.base import BaseCommand

from core import audit, crypto


class Command(BaseCommand):
    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument('--skip-evidence', action='store_true', help='Do not re-hash evidence files.')

    def handle(self, *args, **options):
        from elections.integrity import evidence_intact, verify_snapshot
        from elections.models import ElectionConfigSnapshot, EvidenceItem, ResultCertification
        from elections.results import bulletin_trackers, result_hash

        failures = 0

        def report(ok, label):
            nonlocal failures
            if not ok:
                failures += 1
            self.stdout.write(f'[{"OK" if ok else "FAIL"}] {label}')

        for chain, (ok, count, bad_seq, message) in audit.verify_all().items():
            report(ok, f'audit chain {chain}: {count} entries - {message}')
        for snapshot in ElectionConfigSnapshot.objects.all():
            report(verify_snapshot(snapshot), f'config snapshot election={snapshot.election_id} v{snapshot.version}')
        for cert in ResultCertification.objects.filter(revoked_at__isnull=True).select_related('result'):
            encoded = crypto.canonical_json(cert.payload)
            report(crypto.verify_signature(encoded, cert.signature, cert.public_key),
                   f'certification signature election={cert.election_id}')
            report(result_hash(cert.result.data) == cert.payload['result_hash'],
                   f'certified result hash election={cert.election_id}')
            report(crypto.merkle_root(bulletin_trackers(cert.election)) == cert.payload['bulletin_root'],
                   f'bulletin board root election={cert.election_id}')
        self._check_keys(report)
        if not options['skip_evidence']:
            for item in EvidenceItem.objects.all():
                try:
                    ok = evidence_intact(item)
                except FileNotFoundError:
                    ok = False
                report(ok, f'evidence #{item.pk} {item.title}')
        if failures:
            self.stderr.write(self.style.ERROR(f'{failures} integrity check(s) FAILED'))
            sys.exit(1)
        self.stdout.write(self.style.SUCCESS('All integrity checks passed.'))

    SAMPLE = 50

    def _check_keys(self, report):
        from core.management.commands.keys import encrypted_columns
        from core.models import DataKey

        for data_key in DataKey.objects.all():
            try:
                crypto.unwrap_data_key(data_key)
                ok = True
            except Exception:  # noqa: BLE001 - any failure means the key is unusable
                ok = False
            report(ok, f'data key {data_key.pk} unwraps ({data_key.provider}:{data_key.kek_id})')
        for model, fields in encrypted_columns():
            for field in fields:
                # Reading through the ORM decrypts each value; wrong keys raise.
                rows = model._base_manager.exclude(**{f'{field.attname}__isnull': True}).order_by('-pk')
                try:
                    values = list(rows.values_list(field.attname, flat=True)[:self.SAMPLE])
                    count = sum(1 for value in values if value not in ('', None))
                    ok = True
                except Exception:  # noqa: BLE001
                    count, ok = 0, False
                if ok and not count:
                    continue
                report(ok, f'decrypt {model._meta.label}.{field.name} ({count} sampled)')
