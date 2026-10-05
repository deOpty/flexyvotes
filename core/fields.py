"""Model fields that transparently envelope-encrypt their values at rest."""
import json

from django.db import models

from . import crypto


class EncryptedTextField(models.TextField):
    """TextField stored as an AES-GCM envelope ciphertext.

    Equality lookups on the ciphertext never match (random nonces); pair the
    field with a blind-index column when it must be searchable.
    """

    description = 'Encrypted text'

    def contribute_to_class(self, cls, name, *args, **kwargs):
        super().contribute_to_class(cls, name, *args, **kwargs)
        # Binding the ciphertext to its column prevents copy/paste of a
        # ciphertext from one field into another.
        self._aad = f'{cls._meta.app_label}.{cls._meta.model_name}.{name}'

    def from_db_value(self, value, expression, connection):
        if value is None or value == '':
            return value
        return crypto.decrypt_str(value, aad=self._aad)

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if value is None or value == '' or crypto.is_encrypted(value):
            return value
        return crypto.encrypt_str(value, aad=self._aad)


class EncryptedJSONField(EncryptedTextField):
    description = 'Encrypted JSON'

    def from_db_value(self, value, expression, connection):
        plaintext = super().from_db_value(value, expression, connection)
        if plaintext in (None, ''):
            return None
        return json.loads(plaintext)

    def to_python(self, value):
        if isinstance(value, (dict, list)) or value is None:
            return value
        return json.loads(value)

    def get_prep_value(self, value):
        if value is None:
            return None
        if isinstance(value, str) and crypto.is_encrypted(value):
            return value
        return crypto.encrypt_str(json.dumps(value, default=str), aad=self._aad)
