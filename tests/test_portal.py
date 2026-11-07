# Assisted by Claude Opus
"""Unit tests for portal.py. Run from the repository root:

    python3 -m unittest discover -s tests -v

Most tests need nothing but Python: psycopg is replaced by a stand-in when it
isn't installed, and the database calls they touch are replaced per test. The
tests in DatabaseTests run only when MAAS_TEST_DATABASE_URL points at a
throwaway PostgreSQL (with psycopg[binary,pool] installed), for example:

    podman run --rm -d --name maas-test-pg -p 55432:5432 \\
      -e POSTGRESQL_USER=maas -e POSTGRESQL_PASSWORD=test -e POSTGRESQL_DATABASE=maas \\
      registry.redhat.io/rhel9/postgresql-16@sha256:9223bb5f0745e66f0e4204ff87d4d1a5283f3726e9b7d9b71d7bdb2a650fc218
    MAAS_TEST_DATABASE_URL=postgresql://maas:test@127.0.0.1:55432/maas python3 -m unittest discover -s tests
"""
import datetime
import http.client
import importlib.util
import json
import os
import sys
import threading
import time
import types
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEST_DB = os.environ.get("MAAS_TEST_DATABASE_URL")


def _stub_psycopg():
    """Stand-ins for psycopg and psycopg_pool, enough to import portal.py."""
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
    if TEST_DB:
        raise
    _stub_psycopg()

os.environ.setdefault("MODE", "keys")
os.environ.setdefault("DATABASE_URL", TEST_DB or "postgresql://unused")
_spec = importlib.util.spec_from_file_location("portal", os.path.join(ROOT, "roles/maas_portals/files/portal.py"))
portal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(portal)

KEY = "maas-" + "A" * 43
FUTURE = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
HEADERS = [("x-maas-user", "u@example.com"), ("x-maas-plan", "p"), ("x-maas-models", "maas-sim"),
           ("x-maas-key-id", "k"), ("x-maas-window", "60"), ("x-llm-d-inference-fairness-id", "u@example.com")]


# ── the strict body check (sections 36 and 37) ──────────────────────────────

class RequestFieldsTests(unittest.TestCase):
    BAD = [
        '{"model":"maas-sim","STREAM":true,"messages":[{"role":"user","content":"hi"}]}',
        '{"model":"maas-sim","MODEL":"other","messages":[{"role":"user","content":"hi"}]}',
        '{"model":"maas-sim","stream":true,"stream_options":{"include_usage":true},"stream_options":{},"messages":[]}',
        '{"model":"maas-sim","stream":true,"stream_options":{"include_usage":"false"},"messages":[]}',
        '{"model":"maas-sim","stream":true,"stream_options":{"include_usage":true,"Include_Usage":false},"messages":[]}',
        '{"model":"maas-sim","stream":"true"}',
        '{"model":1}',
        '{"model":"m","max_tokens":true}',
        '{"model":"m","max_tokens":1.5}',
        '[{"model":"m"}]',
        'not json',
    ]

    def test_ambiguous_or_mistyped_bodies_are_refused(self):
        for body in self.BAD:
            with self.subTest(body=body), self.assertRaises(portal.BodyError):
                portal.request_fields(body)

    def test_normal_bodies_pass(self):
        f = portal.request_fields('{"model":"maas-sim","messages":[{"role":"user","content":"hi"}]}')
        self.assertEqual((f["model"], f["stream"], f["include_usage"]), ("maas-sim", False, False))
        f = portal.request_fields('{"model":"maas-sim","stream":true,"stream_options":{"include_usage":true}}')
        self.assertEqual((f["stream"], f["include_usage"]), (True, True))
        f = portal.request_fields('{"model":"m","max_tokens":100,"max_completion_tokens":50}')
        self.assertEqual((f["max_tokens"], f["max_completion_tokens"]), (100, 50))

    def test_nested_fields_are_not_checked(self):
        body = json.dumps({"model": "maas-sim", "messages": [{"role": "user", "content": "hi", "model": "x"}],
                           "tools": [{"type": "function", "function": {"name": "f", "parameters": {
                               "properties": {"Stream": {"type": "boolean"}, "stream": {"type": "string"}}}}}]})
        self.assertEqual(portal.request_fields(body)["model"], "maas-sim")

    def test_huge_bodies_are_refused_quickly(self):
        for body in ('{"model":"m","messages":[' + ",".join(['"a"'] * 1_000_000) + ']}',
                     '{"model":"m","messages":[' + ",".join(["1"] * 2_000_000) + ']}',
                     '{"model":"m","messages":[' + ",".join(["{}"] * 500_000) + ']}'):
            start = time.monotonic()
            with self.assertRaises(portal.BodyError):
                portal.request_fields(body)
            self.assertLess(time.monotonic() - start, 2)

    def test_one_long_string_is_fine(self):
        self.assertEqual(portal.request_fields('{"model":"m","messages":["' + "a" * 4_000_000 + '"]}')["model"], "m")


# ── the key check handler ────────────────────────────────────────────────────

class ValidateTests(unittest.TestCase):
    """Validate over plain HTTP on a free port, with lookup and Limitador stubbed."""

    @classmethod
    def setUpClass(cls):
        cls.srv = portal.serve(portal.Validate, "127.0.0.1", 0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        portal.SERVERS.remove(cls.srv)

    def setUp(self):
        with portal.CACHE_LOCK:
            portal.CACHE.clear()
            portal.MISSES.clear()
        self.limits = {"max_request_tokens": None}
        self.plans = {"p"}
        patches = [mock.patch.object(portal, "lookup", side_effect=lambda h: (FUTURE, HEADERS, self.limits)),
                   mock.patch.object(portal, "limitador_plans", side_effect=lambda: self.plans),
                   mock.patch.object(portal, "db", return_value=None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def post(self, body, key=KEY, path="/check/body/v1/chat/completions", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        data = body.encode() if isinstance(body, str) else body
        h = {"Authorization": "Bearer " + key, "Content-Type": "application/json", **(headers or {})}
        c.request("POST", path, body=data, headers=h)
        r = c.getresponse()
        out = r.status, r.read().decode()
        c.close()
        return out

    def test_valid_request(self):
        self.assertEqual(self.post('{"model":"maas-sim"}')[0], 200)

    def test_malformed_key(self):
        self.assertEqual(self.post('{"model":"maas-sim"}', key="maas-short")[0], 401)

    def test_model_outside_plan(self):
        self.assertEqual(self.post('{"model":"other"}')[0], 403)

    def test_upper_case_stream_is_refused(self):
        self.assertEqual(self.post('{"model":"maas-sim","STREAM":true}')[0], 400)

    def test_stream_without_usage_is_refused(self):
        self.assertEqual(self.post('{"model":"maas-sim","stream":true}')[0], 400)

    def test_plan_not_loaded_is_403(self):
        self.plans = set()
        self.assertEqual(self.post('{"model":"maas-sim"}'), (403, "plan not active yet"))

    def test_unknown_limits_is_503(self):
        self.plans = None
        self.assertEqual(self.post('{"model":"maas-sim"}')[0], 503)

    def test_per_request_cap(self):
        self.limits = {"max_request_tokens": 100}
        self.assertEqual(self.post('{"model":"maas-sim","max_tokens":100}')[0], 200)
        self.assertEqual(self.post('{"model":"maas-sim","max_tokens":101}')[0], 400)
        self.assertEqual(self.post('{"model":"maas-sim","max_completion_tokens":101}')[0], 400)
        self.assertEqual(self.post('{"model":"maas-sim"}')[0], 400)

    def test_body_over_limit_is_413_without_reading(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest("POST", "/check/body/x")
        c.putheader("Authorization", "Bearer " + KEY)
        c.putheader("Content-Length", str(portal.BODY_MAX_BYTES + 1))
        c.endheaders()
        self.assertEqual(c.getresponse().status, 413)
        c.close()

    def test_busy_slots_answer_503(self):
        held = [portal.LARGE_BODY_SLOTS.acquire(timeout=1) for _ in range(2)]
        try:
            big = '{"model":"maas-sim","messages":["' + "a" * (portal.SMALL_BODY_BYTES + 10) + '"]}'
            self.assertEqual(self.post(big)[0], 503)
            self.assertEqual(self.post('{"model":"maas-sim"}')[0], 200)  # the small lane is free
        finally:
            for ok in held:
                if ok:
                    portal.LARGE_BODY_SLOTS.release()

    def test_last_used_write_failure_still_answers(self):
        with portal.LAST_USED_LOCK:
            portal.LAST_USED.clear()
        portal.db.side_effect = RuntimeError("database down")
        self.assertEqual(self.post('{"model":"maas-sim"}')[0], 200)
        with portal.LAST_USED_LOCK:
            self.assertNotIn(portal.hash_value(KEY), portal.LAST_USED)

    def test_database_down_serves_stale_result(self):
        self.assertEqual(self.post('{"model":"maas-sim"}')[0], 200)
        h = portal.hash_value(KEY)
        with portal.CACHE_LOCK:
            exp, value = portal.CACHE[h]
            portal.CACHE[h] = (time.time() - 1, value)  # due for a refresh
        portal.lookup.side_effect = RuntimeError("database down")
        self.assertEqual(self.post('{"model":"maas-sim"}')[0], 200)
        other = "maas-" + "B" * 43
        self.assertEqual(self.post('{"model":"maas-sim"}', key=other)[0], 503)

    def test_burst_of_checks(self):
        results = []

        def one():
            results.append(self.post('{"model":"maas-sim"}')[0])
        ts = [threading.Thread(target=one) for _ in range(300)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(results.count(200), 300)


# ── smaller pieces ───────────────────────────────────────────────────────────

class FakeHandler:
    cookie_value = portal.Base.cookie_value
    from_loopback_name = portal.Base.from_loopback_name

    def __init__(self, **headers):
        self.headers = headers


class SmallTests(unittest.TestCase):
    def test_key_format(self):
        self.assertTrue(portal.KEY_RE.fullmatch(portal.new_key()))
        self.assertFalse(portal.KEY_RE.fullmatch("maas-" + "A" * 42))

    def test_cookie_parsing_ignores_tossed_and_malformed_cookies(self):
        h = FakeHandler(Cookie='theme="dark; maas_session=x; __Host-maas_session=good; other=1')
        self.assertEqual(h.cookie_value("__Host-maas_session"), "good")

    def test_loopback_names(self):
        self.assertTrue(FakeHandler(Host="localhost:8081").from_loopback_name())
        self.assertTrue(FakeHandler(Host="[::1]:8081").from_loopback_name())
        self.assertFalse(FakeHandler(Host="attacker.example").from_loopback_name())

    def test_plan_checks_run_before_the_database(self):
        with mock.patch.object(portal, "db") as db:
            for args in ((("x'y",), {}), (("p",), {"priority": 5000}), (("p",), {"max_request_tokens": 0})):
                (name,), kw = args
                with self.subTest(kw=kw), self.assertRaises(ValueError):
                    portal.set_plan("t", name, [], 10, 60, 1, **kw)
            db.assert_not_called()

    def test_limits_yaml_adds_request_limits(self):
        with mock.patch.object(portal, "db", return_value=[("a", 100, 60, None, None), ("b", 200, 60, 5, None)]):
            out = json.loads(portal.limits_yaml())
        self.assertEqual([(lim["namespace"], lim["max_value"]) for lim in out],
                         [("maas", 100), ("maas", 200), ("maas-requests", 5)])

    def test_hourly_cap(self):
        self.assertEqual(portal.hourly_cap(24000, 86400, None), 2000)  # default for a daily plan
        self.assertIsNone(portal.hourly_cap(20000, 60, None))          # short windows: none
        self.assertIsNone(portal.hourly_cap(24000, 86400, 0))          # turned off
        self.assertEqual(portal.hourly_cap(24000, 86400, 500), 500)
        with mock.patch.object(portal, "db", return_value=[("d", 24000, 86400, None, None)]):
            out = json.loads(portal.limits_yaml())
        self.assertEqual([(lim["seconds"], lim["max_value"]) for lim in out], [(86400, 24000), (3600, 2000)])

    def test_optional_int(self):
        self.assertIsNone(portal.optional_int(""))
        self.assertIsNone(portal.optional_int(None))
        self.assertEqual(portal.optional_int("7"), 7)


class ReconcilerTests(unittest.TestCase):
    def test_refuses_to_revoke_most_users_in_one_pass(self):
        rows = [(f"id{i}", f"u{i}@example.com") for i in range(10)]
        with mock.patch.object(portal, "db", return_value=rows), \
                mock.patch.object(portal, "kc_token", return_value="t"), \
                mock.patch.object(portal, "kc_get", return_value=None), \
                mock.patch.object(portal, "revoke") as revoke, \
                mock.patch.object(portal, "reconcile") as reconcile, \
                mock.patch.object(portal.time, "sleep", side_effect=StopIteration):
            with self.assertRaises(StopIteration):
                portal.reconciler()
        revoke.assert_not_called()
        reconcile.assert_not_called()
        self.assertEqual(portal.RECONCILER["refused"], 10)


class SigtermTests(unittest.TestCase):
    def test_exits_while_starting(self):
        with mock.patch.object(portal, "SERVERS", []), self.assertRaises(SystemExit):
            portal.on_sigterm()


# ── against a real PostgreSQL ────────────────────────────────────────────────

@unittest.skipUnless(TEST_DB, "set MAAS_TEST_DATABASE_URL to a throwaway PostgreSQL")
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        portal.POOL = portal.ConnectionPool(TEST_DB, min_size=1, max_size=40, timeout=10,
                                            kwargs={"autocommit": True}, open=True)
        portal.wait_for_db()
        portal.wait_for_db()  # a second start runs no migration
        portal.notify = lambda payload: None

    def setUp(self):
        for t in ("audit", "api_keys", "group_plans", "plans"):
            portal.db(f"DELETE FROM {t}")

    def plan(self, name, tokens, window, priority=0):
        portal.db("INSERT INTO plans (name, tokens, window_seconds, priority) VALUES (%s, %s, %s, %s)",
                  (name, tokens, window, priority))

    def test_effective_prefers_priority_then_rate(self):
        self.plan("daily", 100000, 86400)
        self.plan("standard", 20000, 60)
        portal.db("INSERT INTO group_plans VALUES ('a', 'daily'), ('b', 'standard')")
        self.assertEqual(portal.effective(["a", "b"])[0], "standard")
        portal.db("UPDATE plans SET priority = 10 WHERE name = 'daily'")
        eff = portal.effective(["a", "b"])
        self.assertEqual((eff[0], eff[5]), ("daily", ["a", "b"]))

    def test_large_budget_fits(self):
        self.plan("big", 5_000_000_000, 2678400)

    def test_concurrent_creates_respect_the_cap(self):
        self.plan("p", 100, 60)
        go = threading.Barrier(30)

        def one():
            go.wait()
            try:
                portal.create_key("t", uuid.uuid4(), "service", "service:race", None, "k", None, "p",
                                  portal.new_key(), 1, {})
            except ValueError:
                pass
        ts = [threading.Thread(target=one) for _ in range(30)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        n = portal.db("SELECT count(*) FROM api_keys WHERE owner = 'service:race'", fetch=True)[0][0]
        self.assertEqual(n, portal.KEY_LIMIT)

    def test_hourly_limit(self):
        self.plan("p", 100, 60)
        for i in range(portal.KEYS_PER_HOUR):
            portal.create_key("t", uuid.uuid4(), "service", "service:hourly", None, "k", None, "p",
                              portal.new_key(), 1, {})
            portal.db("UPDATE api_keys SET revoked_at = now() WHERE owner = 'service:hourly'")
        with self.assertRaisesRegex(ValueError, "per hour"):
            portal.create_key("t", uuid.uuid4(), "service", "service:hourly", None, "k", None, "p",
                              portal.new_key(), 1, {})


if __name__ == "__main__":
    unittest.main()
