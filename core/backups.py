"""Read the status files written by the `backup` worker (deploy/backup/fv-backup.sh).

The worker shares its state volume read-only with the web container, so the
console can show whether nightly backups and GitHub pushes are current.
"""
from datetime import datetime, timezone as dt_timezone
from pathlib import Path

from django.conf import settings


def _read(path):
    try:
        return path.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def _timestamp(path):
    value = _read(path)
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc) if value else None
    except (ValueError, OverflowError, OSError):
        return None


def backup_status(now=None):
    state = Path(settings.BACKUP_STATE_DIR)
    now = now or datetime.now(dt_timezone.utc)
    max_age = settings.BACKUP_MAX_AGE_HOURS * 3600
    last_backup = _timestamp(state / 'last_backup')
    last_push = _timestamp(state / 'last_push')
    github = bool(settings.BACKUP_GIT_REPO)

    def fresh(moment):
        return bool(moment) and (now - moment).total_seconds() <= max_age

    return {
        'available': state.is_dir(),
        'last_backup': last_backup,
        'last_backup_name': _read(state / 'last_backup_name'),
        'backup_ok': fresh(last_backup),
        'github_configured': github,
        'last_push': last_push,
        'push_ok': fresh(last_push) if github else None,
        'max_age_hours': settings.BACKUP_MAX_AGE_HOURS,
    }
