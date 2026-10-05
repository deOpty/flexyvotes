"""Test runner that isolates shared state between tests.

The local-memory cache is process-wide, so rate-limit counters and circuit
breaker state would otherwise leak from one test into the next.
"""
from django.core.cache import cache
from django.test import SimpleTestCase
from django.test.runner import DiscoverRunner

_original_setup_and_call = SimpleTestCase._setup_and_call


def _reset_shared_state():
    cache.clear()
    from . import crypto

    crypto._active_dek.clear()
    crypto._dek_cache.clear()


def _setup_and_call(self, result, debug=False):
    _reset_shared_state()
    return _original_setup_and_call(self, result, debug=debug)


class FlexyTestRunner(DiscoverRunner):
    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        SimpleTestCase._setup_and_call = _setup_and_call

    def teardown_test_environment(self, **kwargs):
        SimpleTestCase._setup_and_call = _original_setup_and_call
        super().teardown_test_environment(**kwargs)
