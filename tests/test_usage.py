# Assisted by Claude Opus
"""Unit tests for the usage page. Run from the repository root:

    python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stub_psycopg():
    pg = types.ModuleType("psycopg")
    pg.Error = Exception
    pg.errors = types.SimpleNamespace(ForeignKeyViolation=type("ForeignKeyViolation", (Exception,), {}))
    pg.connect = mock.Mock(side_effect=RuntimeError("no database in unit tests"))
    pg_types = types.ModuleType("psycopg.types")
    pg_json = types.ModuleType("psycopg.types.json")
    pg_json.Jsonb = lambda v: v
    pool = types.ModuleType("psycopg_pool")

    class ConnectionPool:
        def __init__(self, *a, **k):
            pass

        def open(self):
            pass

        def connection(self):
            raise RuntimeError("no database in unit tests")

    pool.ConnectionPool = ConnectionPool
    sys.modules.update({"psycopg": pg, "psycopg.types": pg_types, "psycopg.types.json": pg_json,
                        "psycopg_pool": pool})


try:
    import psycopg  # noqa: F401
    import psycopg_pool  # noqa: F401
except ImportError:
    _stub_psycopg()

os.environ.setdefault("MODE", "usage")
os.environ.setdefault("DATABASE_URL", "postgresql://unused")
_spec = importlib.util.spec_from_file_location("portal", os.path.join(ROOT, "roles/maas_usage/files/portal.py"))
portal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(portal)

SUBS = [
    {"kind": "MaaSSubscription", "metadata": {"name": "free"},
     "spec": {"priority": 0, "owner": {"groups": [{"name": "maas-free"}], "users": []}, "modelRefs": []}},
    {"kind": "MaaSSubscription", "metadata": {"name": "standard"},
     "spec": {"priority": 10, "owner": {"groups": [{"name": "maas-standard"}], "users": ["alice"]}, "modelRefs": []}},
    {"kind": "MaaSModelRef", "metadata": {"name": "maas-sim"}, "spec": {}},
]


class SubscriptionTests(unittest.TestCase):
    def test_by_group(self):
        names = [s["metadata"]["name"] for s in portal.subscriptions(SUBS, ["maas-free"], "bob")]
        self.assertEqual(names, ["free"])

    def test_by_user(self):
        names = [s["metadata"]["name"] for s in portal.subscriptions(SUBS, [], "alice")]
        self.assertEqual(names, ["standard"])

    def test_no_match(self):
        self.assertEqual(portal.subscriptions(SUBS, ["other"], "bob"), [])

    def test_highest_priority_first(self):
        names = [s["metadata"]["name"] for s in portal.subscriptions(SUBS, ["maas-free", "maas-standard"], "bob")]
        self.assertEqual(names, ["standard", "free"])


COUNTERS = [
    {"limit": {"name": "ns-standard-maas-sim-tokens", "max_value": 1000, "seconds": 3600},
     "set_variables": {portal.VARIABLE: "alice"}, "remaining": 400},
    {"limit": {"name": "ns-standard-maas-sim-tokens", "max_value": 1000, "seconds": 3600},
     "set_variables": {portal.VARIABLE: "bob"}, "remaining": 999},
    {"limit": {"name": "ns-free-maas-sim-tokens", "max_value": 1000, "seconds": 3600},
     "set_variables": {portal.VARIABLE: "alice"}, "remaining": 250},
]


class CounterTests(unittest.TestCase):
    def test_matches_name_user_and_rate(self):
        self.assertEqual(portal.counter_used(COUNTERS, "ns-standard-maas-sim-tokens", "alice", 1000, 3600), 600)
        self.assertEqual(portal.counter_used(COUNTERS, "ns-free-maas-sim-tokens", "alice", 1000, 3600), 750)

    def test_other_user_or_rate_is_not_used(self):
        self.assertIsNone(portal.counter_used(COUNTERS, "ns-standard-maas-sim-tokens", "carol", 1000, 3600))
        self.assertIsNone(portal.counter_used(COUNTERS, "ns-standard-maas-sim-tokens", "alice", 1000, 60))

    def test_wrapped_remaining(self):
        counters = [{"limit": {"name": "n", "max_value": 10, "seconds": 60},
                     "set_variables": {portal.VARIABLE: "alice"}, "remaining": 2**64 - 3}]
        self.assertEqual(portal.counter_used(counters, "n", "alice", 10, 60), 13)


class IdentityTests(unittest.TestCase):
    def test_preferred_username(self):
        self.assertEqual(portal.username_from_claims({"preferred_username": "alice", "email": "a@example.com"}),
                         "alice")

    def test_missing_username(self):
        with self.assertRaises(ValueError):
            portal.username_from_claims({"email": "a@example.com"})

    def test_groups_string(self):
        self.assertEqual(portal.groups_from_claims({"groups": "maas-free"}), ["maas-free"])


class VolumeTests(unittest.TestCase):
    def test_volume_stats(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "claim"))
            stats = portal.volume_stats(root)
        self.assertEqual(len(stats), 1)
        labels, free, size = stats[0]
        self.assertEqual(labels, {"volume": "claim"})
        self.assertGreater(size, 0)
        self.assertLessEqual(free, size)


class WindowTests(unittest.TestCase):
    def test_window_seconds(self):
        self.assertEqual(portal.window_seconds("24h"), 86400)
        self.assertEqual(portal.window_seconds("1h"), 3600)
        self.assertIsNone(portal.window_seconds("0h"))


if __name__ == "__main__":
    unittest.main()
