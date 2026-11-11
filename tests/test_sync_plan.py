# Assisted by Claude Opus
"""Unit tests for the users and subscriptions planner. Run from the
repository root:

    python3 -m unittest discover -s tests -v
"""
import importlib.util
import os
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "sync_plan", os.path.join(ROOT, "roles/maas_sync/files/sync_plan.py"))
sync_plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync_plan)


def write(directory, name, text):
    path = os.path.join(directory, name)
    with open(path, "w") as f:
        f.write(text)
    return path


GOV = [
    {"kind": "MaaSModelRef", "metadata": {"name": "maas-sim"}, "spec": {}},
    {"kind": "MaaSSubscription", "metadata": {"name": "tui-chat"},
     "spec": {"priority": 30,
              "owner": {"groups": [{"name": "tui-chat"}], "users": []},
              "modelRefs": [{"name": "maas-sim",
                             "tokenRateLimits": [{"limit": 10000, "window": "60s"}]}]}},
]

SUB = {"name": "tui-chat", "groups": ["tui-chat"], "limits": ["10000/60s"],
       "priority": 30, "models": None, "users": None}

STATE = {"keycloak": {"groups": ["tui-chat"], "users": {}, "members": {"tui-chat": []}}}


def run(subs=None, users=None, gov=None, state=None, prune=False):
    return sync_plan.plan(subs if subs is not None else [SUB],
                          users if users is not None else [],
                          gov if gov is not None else GOV,
                          state if state is not None else STATE, prune)


class FileTests(unittest.TestCase):
    def test_desired_files(self):
        with tempfile.TemporaryDirectory() as d:
            s = write(d, "s.yaml", """
subscriptions:
  - name: tui-chat
    groups: [tui-chat]
    limits: 10000/60s
    priority: 30
""")
            u = write(d, "u.yaml", """
users:
  - email: ada@example.com
    subscription: tui-chat
""")
            errors = []
            subs = sync_plan.desired_subscriptions(s, errors)
            users = sync_plan.desired_users(u, subs, errors)
            self.assertEqual(errors, [])
            self.assertEqual(subs[0]["limits"], ["10000/60s"])
            self.assertIsNone(subs[0]["models"])
            self.assertEqual(users, [{"email": "ada@example.com", "subscription": "tui-chat"}])

    def test_bad_limit_and_unknown_subscription(self):
        with tempfile.TemporaryDirectory() as d:
            s = write(d, "s.yaml", """
subscriptions:
  - {name: tui-chat, groups: [tui-chat], limits: 10000, priority: 30}
""")
            u = write(d, "u.yaml", """
users:
  - {email: ada@example.com, subscription: missing}
""")
            errors = []
            subs = sync_plan.desired_subscriptions(s, errors)
            sync_plan.desired_users(u, subs, errors)
            self.assertTrue(any("<limit>/<window>" in e for e in errors))
            self.assertTrue(any("is not in subscriptions.yaml" in e for e in errors))

    def test_duplicate_email(self):
        with tempfile.TemporaryDirectory() as d:
            s = write(d, "s.yaml", "subscriptions: [{name: tui-chat, groups: [tui-chat], limits: 1/60s}]")
            u = write(d, "u.yaml", """
users:
  - {email: ada@example.com, subscription: tui-chat}
  - {email: ADA@example.com, subscription: tui-chat}
""")
            errors = []
            subs = sync_plan.desired_subscriptions(s, errors)
            sync_plan.desired_users(u, subs, errors)
            self.assertTrue(any("duplicate email" in e for e in errors))


class PlanTests(unittest.TestCase):
    def test_unchanged(self):
        out = run()
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["changes"], [])
        self.assertEqual(out["removals"], [])
        self.assertEqual(out["pending"], [])

    def test_new_subscription_and_group(self):
        sub = {"name": "new", "groups": ["new"], "limits": ["5/60s"],
               "priority": 10, "models": None, "users": None}
        out = run(subs=[SUB, sub])
        kinds = [c["kind"] for c in out["changes"]]
        self.assertIn("group-create", kinds)
        self.assertIn("subscription-set", kinds)
        self.assertEqual(len(out["removals"]), 0)

    def test_limits_change(self):
        sub = dict(SUB, limits=["20000/60s"])
        out = run(subs=[sub])
        self.assertEqual([c["kind"] for c in out["changes"]], ["subscription-set"])
        self.assertIn("limits", out["changes"][0]["description"])

    def test_direct_users_are_preserved(self):
        gov = [dict(GOV[0]),
               dict(GOV[1], spec=dict(GOV[1]["spec"], owner={"groups": [{"name": "tui-chat"}],
                                                             "users": ["alice"]}))]
        self.assertEqual(run(gov=gov)["changes"], [])

    def test_direct_users_are_cleared_when_listed_empty(self):
        gov = [dict(GOV[0]),
               dict(GOV[1], spec=dict(GOV[1]["spec"], owner={"groups": [{"name": "tui-chat"}],
                                                             "users": ["alice"]}))]
        out = run(subs=[dict(SUB, users=[])], gov=gov)
        self.assertIn("direct users", out["changes"][0]["description"])

    def test_membership_add(self):
        state = {"keycloak": {"groups": ["tui-chat"],
                              "users": {"ada@example.com": {"exists": True, "groups": []}},
                              "members": {"tui-chat": []}}}
        out = run(users=[{"email": "ada@example.com", "subscription": "tui-chat"}], state=state)
        self.assertEqual([c["kind"] for c in out["changes"]], ["membership-add"])

    def test_membership_satisfied_by_another_group(self):
        sub = dict(SUB, groups=["g1", "g2"])
        gov = [GOV[0], {"kind": "MaaSSubscription", "metadata": {"name": "tui-chat"},
                        "spec": {"priority": 30, "owner": {"groups": [{"name": "g1"}, {"name": "g2"}],
                                                           "users": []},
                                 "modelRefs": [{"name": "maas-sim",
                                                "tokenRateLimits": [{"limit": 10000, "window": "60s"}]}]}}]
        state = {"keycloak": {"groups": ["g1", "g2"],
                              "users": {"ada@example.com": {"exists": True, "groups": ["g2"]}},
                              "members": {"g1": [], "g2": ["ada,ada@example.com"]}}}
        out = run(subs=[sub], users=[{"email": "ada@example.com", "subscription": "tui-chat"}],
                  gov=gov, state=state)
        self.assertEqual(out["changes"], [])

    def test_pending_user(self):
        state = {"keycloak": {"groups": ["tui-chat"],
                              "users": {"ada@example.com": {"exists": False, "groups": []}},
                              "members": {"tui-chat": []}}}
        out = run(users=[{"email": "ada@example.com", "subscription": "tui-chat"}], state=state)
        self.assertEqual(out["changes"], [])
        self.assertEqual(len(out["pending"]), 1)

    def test_extra_membership_and_subscription(self):
        gov = GOV + [{"kind": "MaaSSubscription", "metadata": {"name": "free"},
                      "spec": {"priority": 0, "owner": {"groups": [{"name": "free"}], "users": []},
                               "modelRefs": [{"name": "maas-sim",
                                              "tokenRateLimits": [{"limit": 1, "window": "60s"}]}]}}]
        state = {"keycloak": {"groups": ["tui-chat"], "users": {},
                              "members": {"tui-chat": ["bob,bob@example.com"]}}}
        out = run(gov=gov, state=state)
        kinds = [r["kind"] for r in out["removals"]]
        self.assertIn("membership-remove", kinds)
        self.assertIn("subscription-delete", kinds)

    def test_priority_collision(self):
        gov = GOV + [{"kind": "MaaSSubscription", "metadata": {"name": "free"},
                      "spec": {"priority": 30, "owner": {"groups": [{"name": "free"}], "users": []},
                               "modelRefs": [{"name": "maas-sim",
                                              "tokenRateLimits": [{"limit": 1, "window": "60s"}]}]}}]
        self.assertTrue(any("priority 30" in e for e in run(gov=gov)["errors"]))
        self.assertEqual(run(gov=gov, prune=True)["errors"], [])

    def test_unknown_model(self):
        out = run(subs=[dict(SUB, models=["missing"])])
        self.assertTrue(any("unknown model" in e for e in out["errors"]))
        self.assertEqual(out["changes"], [])


if __name__ == "__main__":
    unittest.main()
