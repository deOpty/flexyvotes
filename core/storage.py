import hashlib

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.functional import LazyObject


class PrivateStorage(LazyObject):
    """Filesystem storage that is never exposed through a public URL."""

    def _setup(self):
        self._wrapped = FileSystemStorage(location=str(settings.PRIVATE_MEDIA_ROOT), base_url=None)


private_storage = PrivateStorage()


def file_sha256(uploaded):
    digest = hashlib.sha256()
    for chunk in uploaded.chunks():
        digest.update(chunk)
    uploaded.seek(0)
    return digest.hexdigest()


ALLOWED_DOCUMENT_TYPES = {
    '.pdf': b'%PDF',
    '.png': b'\x89PNG',
    '.jpg': b'\xff\xd8\xff',
    '.jpeg': b'\xff\xd8\xff',
}


def validate_document(uploaded, max_bytes=10 * 1024 * 1024, allowed=ALLOWED_DOCUMENT_TYPES):
    """Check size, extension and magic bytes; returns an error string or None."""
    from pathlib import Path

    if uploaded is None:
        return 'No file uploaded.'
    if uploaded.size > max_bytes:
        return f'File too large (max {max_bytes // (1024 * 1024)} MB).'
    ext = Path(uploaded.name).suffix.lower()
    if ext not in allowed:
        return 'Unsupported file type. Allowed: ' + ', '.join(sorted(allowed))
    head = uploaded.read(8)
    uploaded.seek(0)
    if not head.startswith(allowed[ext]):
        return 'File content does not match its extension.'
    return None
