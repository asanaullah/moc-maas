#!/usr/bin/env python3
# Assisted by Claude Opus
"""Plan the MaaS users and subscriptions from two YAML files.

  sync_plan.py --subscriptions s.yaml --users u.yaml --requirements
  sync_plan.py --subscriptions s.yaml --users u.yaml \\
               --governance objects.yaml --state state.json [--prune]

The first form prints the Keycloak groups and user emails the playbook has to
query. The second prints the plan:

  errors    the files are invalid; the playbook stops
  changes   add or update actions, always applied
  removals  extra state, applied only with --prune (or reported as drift)
  pending   users with no Keycloak account yet; they must sign in once
  warnings  members the sync cannot identify by email, left alone

A plan is always printed with exit code 0, even when it carries errors; the
playbook reads the lists.
"""
import argparse
import json
import re

import yaml

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
GROUP_RE = re.compile(r"^[A-Za-z0-9._-]+$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._@+-]+$")


def load_list(path, key, errors):
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as e:
        errors.append(f"cannot read {path}: {e}")
        return []
    if not isinstance(data, dict):
        errors.append(f"{path} must be a mapping with a '{key}' list")
        return []
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"{path}: '{key}' must be a list")
        return []
    return value


def parse_limits(value):
    parts = value if isinstance(value, list) else str(value or "").split(",")
    limits = []
    for part in parts:
        part = str(part).strip()
        if not part:
            continue
        limit, sep, window = part.partition("/")
        if not sep or not limit.isdigit() or not window:
            raise ValueError(f"limit {part!r} must look like <limit>/<window>, e.g. 24000/24h")
        limits.append(f"{int(limit)}/{window}")
    if not limits:
        raise ValueError("set at least one limit")
    return limits


def parse_list(value, field, errors, name):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        errors.append(f"{name}: {field} must be a list or a comma-separated string")
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def desired_subscriptions(path, errors):
    out = []
    seen = set()
    for n, raw in enumerate(load_list(path, "subscriptions", errors), 1):
        if not isinstance(raw, dict):
            errors.append(f"subscriptions[{n}] must be a mapping")
            continue
        name = str(raw.get("name") or "").strip()
        label = f"subscription {name or '#' + str(n)}"
        if not NAME_RE.match(name):
            errors.append(f"{label}: name must be a DNS label")
            continue
        if name in seen:
            errors.append(f"{label}: duplicate name")
            continue
        seen.add(name)
        groups = parse_list(raw.get("groups"), "groups", errors, label) or []
        if not groups:
            errors.append(f"{label}: set at least one group")
            continue
        bad = [g for g in groups if not GROUP_RE.match(g)]
        if bad:
            errors.append(f"{label}: invalid group name(s): {', '.join(bad)}")
            continue
        try:
            limits = parse_limits(raw.get("limits"))
        except ValueError as e:
            errors.append(f"{label}: {e}")
            continue
        priority = raw.get("priority", 0)
        try:
            priority = int(priority)
            if priority < 0:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"{label}: priority must be a non-negative integer")
            continue
        models = parse_list(raw.get("models"), "models", errors, label)
        if models == []:
            # An explicit empty list is the same as omitting models.
            models = None
        users = parse_list(raw.get("users"), "users", errors, label)
        out.append({"name": name, "groups": groups, "limits": limits,
                    "priority": priority, "models": models, "users": users})
    return out


def desired_users(path, subscriptions, errors):
    by_name = {s["name"]: s for s in subscriptions}
    out = []
    seen = set()
    for n, raw in enumerate(load_list(path, "users", errors), 1):
        if not isinstance(raw, dict):
            errors.append(f"users[{n}] must be a mapping")
            continue
        email = str(raw.get("email") or "").strip()
        label = f"user {email or '#' + str(n)}"
        if not EMAIL_RE.match(email):
            errors.append(f"{label}: email must be letters, digits, dot, underscore, at, plus or dash")
            continue
        if email.lower() in seen:
            errors.append(f"{label}: duplicate email")
            continue
        seen.add(email.lower())
        sub = str(raw.get("subscription") or "").strip()
        if sub not in by_name:
            errors.append(f"{label}: subscription {sub!r} is not in subscriptions.yaml")
            continue
        out.append({"email": email, "subscription": sub})
    return out


def governance_state(objects):
    subs, models = [], []
    for obj in objects or []:
        kind = obj.get("kind")
        if kind == "MaaSModelRef":
            models.append(obj["metadata"]["name"])
        elif kind == "MaaSSubscription":
            spec = obj.get("spec") or {}
            owner = spec.get("owner") or {}
            refs = []
            for ref in spec.get("modelRefs") or []:
                refs.append({"name": ref.get("name", ""),
                             "limits": [f"{int(l['limit'])}/{l['window']}"
                                        for l in ref.get("tokenRateLimits") or []]})
            subs.append({"name": obj["metadata"]["name"],
                         "groups": [g["name"] for g in owner.get("groups") or []],
                         "users": list(owner.get("users") or []),
                         "priority": int(spec.get("priority", 0)),
                         "models": refs})
    return subs, models


def set_action(sub, models, users, reasons=None):
    what = ", ".join(reasons) if reasons else "new"
    return {"kind": "subscription-set", "name": sub["name"],
            "groups": list(sub["groups"]), "limits": ",".join(sub["limits"]),
            "priority": sub["priority"], "models": list(models),
            "users": list(users),
            "description": f"write subscription {sub['name']} ({what})"}


def plan(subscriptions, users, objects, state, prune):
    errors = []
    current, all_models = governance_state(objects)
    if not all_models:
        errors.append("there are no MaaSModelRef objects; deploy a model first")
    current_by_name = {s["name"]: s for s in current}
    desired_by_name = {s["name"]: s for s in subscriptions}
    keycloak = state.get("keycloak") or {}
    kc_groups = set(keycloak.get("groups") or [])
    kc_users = {email.lower(): info
                for email, info in (keycloak.get("users") or {}).items()}
    members = keycloak.get("members") or {}

    # The renderer requires unique priorities across every subscription that
    # will exist after the run.
    final = dict(desired_by_name)
    if not prune:
        for cur in current:
            final.setdefault(cur["name"], cur)
    by_priority = {}
    for sub in final.values():
        by_priority.setdefault(sub["priority"], []).append(sub["name"])
    for priority, names in sorted(by_priority.items()):
        if len(names) > 1:
            errors.append(f"priority {priority} is used by {', '.join(sorted(names))}")

    changes, removals, pending, warnings = [], [], [], []

    desired_groups = {g for s in subscriptions for g in s["groups"]}
    for group in sorted(desired_groups - kc_groups):
        changes.append({"kind": "group-create", "group": group,
                        "description": f"create Keycloak group {group}"})

    for sub in subscriptions:
        cur = current_by_name.get(sub["name"])
        desired_models = sub["models"] if sub["models"] is not None else list(all_models)
        unknown = [m for m in desired_models if m not in all_models]
        if unknown:
            errors.append(f"subscription {sub['name']}: unknown model(s): {', '.join(unknown)}")
            continue
        desired_direct = (sub["users"] if sub["users"] is not None
                          else (list(cur["users"]) if cur else []))
        if cur is None:
            changes.append(set_action(sub, desired_models, desired_direct))
            continue
        reasons = []
        if set(cur["groups"]) != set(sub["groups"]):
            reasons.append("groups")
        if cur["priority"] != sub["priority"]:
            reasons.append("priority")
        if set(cur["users"]) != set(desired_direct):
            reasons.append("direct users")
        if set(m["name"] for m in cur["models"]) != set(desired_models):
            reasons.append("models")
        else:
            for model in cur["models"]:
                if set(model["limits"]) != set(sub["limits"]):
                    reasons.append("limits")
                    break
        if reasons:
            changes.append(set_action(sub, desired_models, desired_direct, reasons))

    for user in users:
        sub = desired_by_name[user["subscription"]]
        info = kc_users.get(user["email"].lower())
        if not info or not info.get("exists"):
            pending.append({"email": user["email"], "subscription": sub["name"],
                            "description": f"{user['email']}: no Keycloak account yet; "
                                           "sign in once, then rerun"})
            continue
        if set(info.get("groups") or []) & set(sub["groups"]):
            continue
        changes.append({"kind": "membership-add", "email": user["email"],
                        "group": sub["groups"][0],
                        "description": f"add {user['email']} to {sub['groups'][0]} "
                                       f"({sub['name']})"})

    # A member of a managed group is wanted when the users file assigns that
    # email to a subscription that includes the group.
    wanted = {}
    for user in users:
        sub = desired_by_name[user["subscription"]]
        for group in sub["groups"]:
            wanted.setdefault(group, set()).add(user["email"].lower())
    for group in sorted(desired_groups):
        for line in members.get(group) or []:
            line = str(line).strip()
            if not line or line.lower().startswith("username,"):
                continue
            username, _, email = line.partition(",")
            username, email = username.strip(), email.strip()
            if not email:
                warnings.append(f"{group}: {username} has no email; left alone")
                continue
            if email.lower() not in wanted.get(group, set()):
                removals.append({"kind": "membership-remove", "email": email,
                                 "group": group,
                                 "description": f"remove {email} from {group} "
                                                "(not in users.yaml)"})

    for cur in current:
        if cur["name"] not in desired_by_name:
            removals.append({"kind": "subscription-delete", "name": cur["name"],
                             "description": f"delete subscription {cur['name']} "
                                            "(not in subscriptions.yaml)"})

    if errors:
        changes, removals, pending = [], [], []
    return {"errors": errors, "changes": changes, "removals": removals,
            "pending": pending, "warnings": warnings,
            "summary": f"{len(changes)} change(s), {len(removals)} removal(s), "
                       f"{len(pending)} pending, {len(warnings)} warning(s)"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--subscriptions", required=True)
    parser.add_argument("--users", required=True)
    parser.add_argument("--requirements", action="store_true")
    parser.add_argument("--governance")
    parser.add_argument("--state")
    parser.add_argument("--prune", action="store_true")
    args = parser.parse_args()

    errors = []
    subscriptions = desired_subscriptions(args.subscriptions, errors)
    users = desired_users(args.users, subscriptions, errors)

    if args.requirements:
        print(json.dumps({
            "errors": errors,
            "groups": sorted({g for s in subscriptions for g in s["groups"]}),
            "emails": sorted({u["email"] for u in users}, key=str.lower),
        }, indent=2))
        return

    if not args.governance or not args.state:
        errors.append("--governance and --state are required without --requirements")
    if errors:
        print(json.dumps({"errors": errors, "changes": [], "removals": [],
                          "pending": [], "warnings": [], "summary": "errors"}, indent=2))
        return

    try:
        with open(args.governance) as f:
            objects = yaml.safe_load(f) or []
        with open(args.state) as f:
            state = json.load(f)
        result = plan(subscriptions, users, objects, state, args.prune)
    except (OSError, yaml.YAMLError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        result = {"errors": [f"cannot read the cluster state: {e}"],
                  "changes": [], "removals": [], "pending": [], "warnings": [],
                  "summary": "errors"}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
