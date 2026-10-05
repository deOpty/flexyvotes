"""Cryptographic primitives used across the platform.

* Envelope encryption: data is encrypted with AES-256-GCM data keys (DEKs);
  DEKs are stored only in wrapped form (``core.DataKey``), wrapped by a
  key-encryption key (KEK) that never touches the database - either local
  keys from the environment / a secrets manager, or AWS KMS.
* Blind indexes: keyed HMACs that allow equality lookups on encrypted fields.
* Ed25519 signatures for election configuration snapshots and results.
* ECIES (X25519 + HKDF + AES-GCM) for sealing ballots to an election key.
* Shamir secret sharing for splitting an election key between trustees.
* Merkle trees for the public ballot bulletin board.
"""
import base64
import hashlib
import hmac
import json
import secrets
import string
import threading
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from django.conf import settings


class CryptoError(Exception):
    pass


# ---------------------------------------------------------------------------
# Encoding helpers
# ---------------------------------------------------------------------------
def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode('ascii').rstrip('=')


def b64d(text: str) -> bytes:
    text = text.strip()
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    raise TypeError(f'Not JSON serializable: {type(value)!r}')


def canonical_json(obj) -> bytes:
    """Deterministic JSON encoding used for anything that gets hashed/signed."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=_json_default).encode('utf-8')


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def hkdf(key_material: bytes, info: bytes, length=32, salt=None) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(key_material)


# ---------------------------------------------------------------------------
# Random values (always the CSPRNG)
# ---------------------------------------------------------------------------
# No 0/O/1/I/L - access codes get read aloud and typed from paper.
ACCESS_CODE_ALPHABET = ''.join(c for c in string.ascii_uppercase + string.digits if c not in '0O1IL')


def random_token(nbytes=32) -> str:
    return secrets.token_urlsafe(nbytes)


def numeric_code(length=6) -> str:
    return ''.join(secrets.choice(string.digits) for _ in range(length))


def access_code(length=10) -> str:
    return ''.join(secrets.choice(ACCESS_CODE_ALPHABET) for _ in range(length))


def keyed_hash(scope: str, value: str) -> str:
    """HMAC-SHA256 keyed by SECRET_KEY - used for one-time tokens/credentials."""
    message = f'{scope}:{value}'.encode('utf-8')
    return hmac.new(settings.SECRET_KEY.encode('utf-8'), message, hashlib.sha256).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest((a or '').encode(), (b or '').encode())


# ---------------------------------------------------------------------------
# Key-encryption-key providers
# ---------------------------------------------------------------------------
DEV_KEK_ID = 'dev'


class LocalKeyProvider:
    """KEKs supplied through the environment (FIELD_ENCRYPTION_KEYS)."""

    name = 'local'

    def __init__(self, spec, secret_key):
        self.keys = {}
        if spec:
            for item in spec.split(','):
                item = item.strip()
                if not item:
                    continue
                kid, _, material = item.partition(':')
                key = b64d(material)
                if len(key) != 32:
                    raise CryptoError(f'KEK "{kid}" must be 32 bytes (base64).')
                self.keys[kid.strip()] = key
            self.active_id = spec.split(',')[0].split(':')[0].strip()
        else:
            # Development fallback - core.checks flags this in production.
            self.active_id = DEV_KEK_ID
        # The SECRET_KEY-derived KEK stays available for *unwrapping* so data
        # keys created before real KEKs were configured can still be read and
        # migrated with `manage.py keys rewrap`. It only wraps when it is active.
        self.keys.setdefault(DEV_KEK_ID, hkdf(secret_key.encode('utf-8'), b'flexyvotes-dev-kek'))

    def wrap(self, dek: bytes):
        nonce = secrets.token_bytes(12)
        wrapped = AESGCM(self.keys[self.active_id]).encrypt(nonce, dek, self.active_id.encode())
        return self.active_id, b64e(nonce + wrapped)

    def unwrap(self, kek_id: str, wrapped: str) -> bytes:
        key = self.keys.get(kek_id)
        if key is None:
            raise CryptoError(f'Unknown key-encryption key "{kek_id}".')
        raw = b64d(wrapped)
        try:
            return AESGCM(key).decrypt(raw[:12], raw[12:], kek_id.encode())
        except InvalidTag as exc:
            raise CryptoError('Data key could not be unwrapped (wrong KEK?).') from exc


class AwsKmsKeyProvider:
    """Wraps data keys with an AWS KMS key so the KEK never leaves the HSM."""

    name = 'aws-kms'

    def __init__(self, key_id, region):
        import boto3  # imported lazily: only needed when KMS is configured

        self.client = boto3.client('kms', region_name=region)
        self.active_id = key_id

    def wrap(self, dek: bytes):
        response = self.client.encrypt(KeyId=self.active_id, Plaintext=dek)
        return response['KeyId'], b64e(response['CiphertextBlob'])

    def unwrap(self, kek_id: str, wrapped: str) -> bytes:
        response = self.client.decrypt(CiphertextBlob=b64d(wrapped), KeyId=kek_id)
        return response['Plaintext']


_provider = None
_provider_lock = threading.Lock()
_dek_cache = {}
_active_dek = {}


def key_provider():
    global _provider
    if _provider is None:
        with _provider_lock:
            if _provider is None:
                if getattr(settings, 'KMS_KEY_ID', None):
                    _provider = AwsKmsKeyProvider(settings.KMS_KEY_ID, settings.AWS_REGION)
                else:
                    _provider = LocalKeyProvider(getattr(settings, 'FIELD_ENCRYPTION_KEYS', None), settings.SECRET_KEY)
    return _provider


def reset_key_caches():
    global _provider
    _provider = None
    _dek_cache.clear()
    _active_dek.clear()


def provider_for(name):
    """The provider able to unwrap data keys wrapped by provider ``name`` -
    lets keys wrapped locally keep working (and be re-wrapped) after a
    switch to KMS."""
    active = key_provider()
    if not name or name == active.name:
        return active
    if name == LocalKeyProvider.name:
        return LocalKeyProvider(getattr(settings, 'FIELD_ENCRYPTION_KEYS', None), settings.SECRET_KEY)
    raise CryptoError(f'Data keys wrapped by "{name}" need that provider configured.')


def unwrap_data_key(data_key) -> bytes:
    return provider_for(data_key.provider).unwrap(data_key.kek_id, data_key.wrapped_key)


def _load_dek(data_key):
    dek = _dek_cache.get(data_key.pk)
    if dek is None:
        dek = unwrap_data_key(data_key)
        _dek_cache[data_key.pk] = dek
    return dek


def active_data_key(purpose='default'):
    """Return (DataKey, dek_bytes) for new encryptions, creating one if needed."""
    from core.models import DataKey

    cached = _active_dek.get(purpose)
    if cached:
        return cached
    data_key = DataKey.objects.filter(purpose=purpose, is_active=True).order_by('-created_at').first()
    if data_key is None:
        data_key = create_data_key(purpose)
    result = (data_key, _load_dek(data_key))
    _active_dek[purpose] = result
    return result


def create_data_key(purpose='default'):
    from core.models import DataKey

    dek = AESGCM.generate_key(bit_length=256)
    kek_id, wrapped = key_provider().wrap(dek)
    DataKey.objects.filter(purpose=purpose, is_active=True).update(is_active=False)
    data_key = DataKey.objects.create(purpose=purpose, kek_id=kek_id, wrapped_key=wrapped, provider=key_provider().name)
    _dek_cache[data_key.pk] = dek
    _active_dek.pop(purpose, None)
    return data_key


ENVELOPE_PREFIX = 'fv1$'


def encrypt_str(plaintext, purpose='default', aad=''):
    if plaintext is None:
        return None
    data_key, dek = active_data_key(purpose)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(dek).encrypt(nonce, str(plaintext).encode('utf-8'), (aad or '').encode('utf-8'))
    return f'{ENVELOPE_PREFIX}{data_key.pk}${b64e(nonce + ct)}'


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


def decrypt_str(token, aad=''):
    from core.models import DataKey

    if token is None or token == '':  # nosec B105
        return token
    if not is_encrypted(token):
        # Legacy/plaintext value written before encryption was enabled.
        return token
    try:
        _, key_id, payload = token.split('$', 2)
        data_key = DataKey.objects.get(pk=int(key_id))
    except (ValueError, DataKey.DoesNotExist) as exc:
        raise CryptoError('Encrypted value references an unknown data key.') from exc
    raw = b64d(payload)
    try:
        return AESGCM(_load_dek(data_key)).decrypt(raw[:12], raw[12:], (aad or '').encode('utf-8')).decode('utf-8')
    except InvalidTag as exc:
        raise CryptoError('Encrypted value failed authentication.') from exc


def data_key_id_of(token):
    if not is_encrypted(token):
        return None
    return int(token.split('$', 2)[1])


# ---------------------------------------------------------------------------
# Blind index (equality search over encrypted columns)
# ---------------------------------------------------------------------------
def _blind_index_key():
    material = getattr(settings, 'BLIND_INDEX_KEY', None)
    if material:
        return b64d(material)
    return hkdf(settings.SECRET_KEY.encode('utf-8'), b'flexyvotes-blind-index')


def blind_index(value, purpose='default'):
    if value is None or str(value).strip() == '':
        return ''
    normalized = str(value).strip().lower()
    return hmac.new(_blind_index_key(), f'{purpose}:{normalized}'.encode('utf-8'), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Platform signatures (Ed25519)
# ---------------------------------------------------------------------------
_signing_key = None


def signing_key() -> Ed25519PrivateKey:
    global _signing_key
    if _signing_key is None:
        material = getattr(settings, 'SIGNING_PRIVATE_KEY', None)
        if material and material.startswith('-----BEGIN'):
            key = serialization.load_pem_private_key(material.replace('\\n', '\n').encode(), password=None)
            if not isinstance(key, Ed25519PrivateKey):
                raise CryptoError('SIGNING_PRIVATE_KEY must be an Ed25519 key.')
            _signing_key = key
        elif material:
            _signing_key = Ed25519PrivateKey.from_private_bytes(b64d(material))
        else:
            _signing_key = Ed25519PrivateKey.from_private_bytes(
                hkdf(settings.SECRET_KEY.encode('utf-8'), b'flexyvotes-dev-signing-key')
            )
    return _signing_key


def reset_signing_key():
    global _signing_key
    _signing_key = None


def public_key_b64(private_key=None) -> str:
    private_key = private_key or signing_key()
    raw = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return b64e(raw)


def key_fingerprint(public_b64: str) -> str:
    return sha256_hex(b64d(public_b64))[:16]


def sign(payload: bytes) -> str:
    return b64e(signing_key().sign(payload))


def verify_signature(payload: bytes, signature_b64: str, public_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(b64d(public_b64)).verify(b64d(signature_b64), payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def trusted_public_keys():
    return [public_key_b64()] + list(getattr(settings, 'SIGNING_PREVIOUS_PUBLIC_KEYS', []))


def generate_signing_keypair():
    key = Ed25519PrivateKey.generate()
    private_raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    return b64e(private_raw), public_key_b64(key)


# ---------------------------------------------------------------------------
# Ballot sealing (ECIES over X25519)
# ---------------------------------------------------------------------------
BALLOT_INFO = b'flexyvotes-ballot-v1'


def generate_election_keypair():
    key = X25519PrivateKey.generate()
    private_raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public_raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return private_raw, b64e(public_raw)


def seal(public_b64: str, plaintext: bytes, aad: bytes = b'') -> str:
    recipient_raw = b64d(public_b64)
    recipient = X25519PublicKey.from_public_bytes(recipient_raw)
    ephemeral = X25519PrivateKey.generate()
    ephemeral_raw = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = hkdf(ephemeral.exchange(recipient) + ephemeral_raw + recipient_raw, BALLOT_INFO)
    nonce = secrets.token_bytes(12)
    return b64e(ephemeral_raw + nonce + AESGCM(key).encrypt(nonce, plaintext, aad))


def unseal(private_raw: bytes, blob_b64: str, aad: bytes = b'') -> bytes:
    raw = b64d(blob_b64)
    if len(raw) < 32 + 12 + 16:
        raise CryptoError('Sealed blob is truncated.')
    private = X25519PrivateKey.from_private_bytes(private_raw)
    recipient_raw = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ephemeral_raw, nonce, ct = raw[:32], raw[32:44], raw[44:]
    key = hkdf(private.exchange(X25519PublicKey.from_public_bytes(ephemeral_raw)) + ephemeral_raw + recipient_raw, BALLOT_INFO)
    try:
        return AESGCM(key).decrypt(nonce, ct, aad)
    except InvalidTag as exc:
        raise CryptoError('Sealed blob failed authentication.') from exc


# ---------------------------------------------------------------------------
# Shamir secret sharing (GF(p), p = 2^521 - 1)
# ---------------------------------------------------------------------------
SHAMIR_PRIME = 2 ** 521 - 1
SHARE_PREFIX = 'fvs1'


def split_secret(secret: bytes, shares: int, threshold: int):
    if not 1 <= threshold <= shares <= 255:
        raise CryptoError('Require 1 <= threshold <= shares <= 255.')
    # 0x01 marker (keeps leading zero bytes) + secret + 8-byte checksum, so a
    # reconstruction from too few or wrong shares is always detected.
    value = int.from_bytes(b'\x01' + secret + hashlib.sha256(secret).digest()[:8], 'big')
    if value >= SHAMIR_PRIME:
        raise CryptoError('Secret too large for the field.')
    coefficients = [value] + [secrets.randbelow(SHAMIR_PRIME) for _ in range(threshold - 1)]
    result = []
    for x in range(1, shares + 1):
        y = 0
        for coefficient in reversed(coefficients):
            y = (y * x + coefficient) % SHAMIR_PRIME
        result.append(f'{SHARE_PREFIX}-{x}-{y:x}')
    return result


def parse_share(share: str):
    try:
        prefix, x, y = share.strip().split('-')
        if prefix != SHARE_PREFIX:
            raise ValueError
        return int(x), int(y, 16)
    except ValueError as exc:
        raise CryptoError('Malformed key share.') from exc


def combine_shares(shares):
    points = [parse_share(s) for s in shares]
    if len({x for x, _ in points}) != len(points):
        raise CryptoError('Duplicate key shares supplied.')
    secret = 0
    for i, (xi, yi) in enumerate(points):
        numerator, denominator = 1, 1
        for j, (xj, _) in enumerate(points):
            if i != j:
                numerator = (numerator * -xj) % SHAMIR_PRIME
                denominator = (denominator * (xi - xj)) % SHAMIR_PRIME
        secret = (secret + yi * numerator * pow(denominator, -1, SHAMIR_PRIME)) % SHAMIR_PRIME
    raw = secret.to_bytes((secret.bit_length() + 7) // 8, 'big')
    if len(raw) < 10 or raw[0] != 1:
        raise CryptoError('Key shares do not reconstruct a valid secret.')
    recovered, checksum = raw[1:-8], raw[-8:]
    if not hmac.compare_digest(hashlib.sha256(recovered).digest()[:8], checksum):
        raise CryptoError('Key shares do not reconstruct a valid secret.')
    return recovered


# ---------------------------------------------------------------------------
# Merkle tree (bulletin board of ballot trackers)
# ---------------------------------------------------------------------------
def _hash_pair(left: str, right: str) -> str:
    return hashlib.sha256(bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def _leaf(value: str) -> str:
    return hashlib.sha256(b'\x00' + bytes.fromhex(value)).hexdigest()


def merkle_root(leaves):
    level = [_leaf(v) for v in leaves]
    if not level:
        return sha256_hex(b'')
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [_hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


def merkle_proof(leaves, index):
    level = [_leaf(v) for v in leaves]
    proof = []
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        sibling = index ^ 1
        proof.append({'hash': level[sibling], 'side': 'left' if sibling < index else 'right'})
        level = [_hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        index //= 2
    return proof


def verify_merkle_proof(value, proof, root):
    current = _leaf(value)
    for step in proof:
        current = _hash_pair(step['hash'], current) if step['side'] == 'left' else _hash_pair(current, step['hash'])
    return hmac.compare_digest(current, root)


# ---------------------------------------------------------------------------
# Passphrase-protected key backups
# ---------------------------------------------------------------------------
def encrypt_with_passphrase(data: bytes, passphrase: str) -> str:
    salt = secrets.token_bytes(16)
    key = Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode('utf-8'))
    nonce = secrets.token_bytes(12)
    return b64e(salt + nonce + AESGCM(key).encrypt(nonce, data, b'flexyvotes-key-backup'))


def decrypt_with_passphrase(blob: str, passphrase: str) -> bytes:
    raw = b64d(blob)
    key = Scrypt(salt=raw[:16], length=32, n=2 ** 15, r=8, p=1).derive(passphrase.encode('utf-8'))
    try:
        return AESGCM(key).decrypt(raw[16:28], raw[28:], b'flexyvotes-key-backup')
    except InvalidTag as exc:
        raise CryptoError('Wrong passphrase or corrupted backup.') from exc
