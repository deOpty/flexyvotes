"""Read-replica routing.

Writes and anything inside a transaction always go to the primary. Code that
can tolerate replica lag (results pages, analytics, reports, exports) opts in
explicitly with ``Model.objects.using(read_db())`` - the vote and payment
paths never do, so they always see their own writes.
"""
from django.conf import settings
from django.db import connections


def read_db():
    if 'replica' in settings.DATABASES and not connections['default'].in_atomic_block:
        return 'replica'
    return 'default'


class ReplicaRouter:
    def db_for_read(self, model, **hints):
        return 'default'

    def db_for_write(self, model, **hints):
        return 'default'

    def allow_relation(self, obj1, obj2, **hints):
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return db == 'default'
