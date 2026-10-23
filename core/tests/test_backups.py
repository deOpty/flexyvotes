import tempfile
import time
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from core.backups import backup_status
from core.tests.factories import make_user


class BackupStatusTests(TestCase):
    """The console reads the status files written by deploy/backup/fv-backup.sh."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, value):
        (self.state / name).write_text(str(value), encoding='utf-8')

    def test_missing_state_directory(self):
        with override_settings(BACKUP_STATE_DIR=str(self.state / 'absent')):
            status = backup_status()
        self.assertFalse(status['available'])
        self.assertFalse(status['backup_ok'])

    def test_fresh_and_stale_backups(self):
        now = int(time.time())
        self.write('last_backup', now - 3600)
        self.write('last_backup_name', 'flexyvotes-20261023T020000Z.dump.enc')
        self.write('last_push', now - 30 * 3600)
        with override_settings(BACKUP_STATE_DIR=str(self.state), BACKUP_GIT_REPO='https://github.com/o/b.git',
                               BACKUP_MAX_AGE_HOURS=26):
            status = backup_status()
        self.assertTrue(status['backup_ok'])
        self.assertEqual(status['last_backup_name'], 'flexyvotes-20261023T020000Z.dump.enc')
        self.assertFalse(status['push_ok'])  # 30 h > 26 h

    def test_push_not_required_without_repository(self):
        self.write('last_backup', int(time.time()))
        with override_settings(BACKUP_STATE_DIR=str(self.state), BACKUP_GIT_REPO=''):
            status = backup_status()
        self.assertIsNone(status['push_ok'])
        self.assertFalse(status['github_configured'])

    def test_garbage_in_status_files_is_ignored(self):
        self.write('last_backup', 'not-a-number')
        with override_settings(BACKUP_STATE_DIR=str(self.state)):
            self.assertIsNone(backup_status()['last_backup'])

    def test_console_health_page_shows_backups(self):
        self.write('last_backup', int(time.time()))
        self.write('last_backup_name', 'flexyvotes-20261023T020000Z.dump.enc')
        self.client.force_login(make_user('ops', staff=True))
        with override_settings(BACKUP_STATE_DIR=str(self.state), BACKUP_GIT_REPO=''):
            page = self.client.get(reverse('console:health'))
        self.assertContains(page, 'flexyvotes-20261023T020000Z.dump.enc')
        self.assertContains(page, 'not configured')
