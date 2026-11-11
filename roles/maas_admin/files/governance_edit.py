#!/usr/bin/env python3
# Assisted by Claude Opus
"""Edit the governance objects file the maas_governance role renders."""

import argparse
import json
import re
import sys

import yaml

NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def load(path):
    with open(path) as f:
        objects = yaml.safe_load(f)
    if not isinstance(objects, list):
        fail("the governance file is not a list")
    return objects


def save(path, objects):
    with open(path, "w") as f:
        yaml.safe_dump(objects, f, sort_keys=False, width=1000)


def find(objects, kind, name):
    return next((o for o in objects if o.get("kind") == kind and o.get("metadata", {}).get("name") == name), None)


def model_names(objects):
    return [o["metadata"]["name"] for o in objects if o.get("kind") == "MaaSModelRef"]


def parse_list(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def parse_limits(value):
    out = []
    for part in parse_list(value):
        limit, sep, window = part.partition("/")
        if not sep or not limit.isdigit():
            fail(f"limit {part!r} must look like <limit>/<window>, e.g. 24000/24h")
        out.append({"limit": int(limit), "window": window})
    if not out:
        fail("set at least one limit, e.g. --limits 24000/24h,2000/1h")
    return out


def subscription_objects(objects, args):
    models = parse_list(args.models) or model_names(objects)
    if not models:
        fail("there are no MaaSModelRef objects; set --models")
    groups = [{"name": g} for g in parse_list(args.groups)]
    users = parse_list(args.users)
    limits = parse_limits(args.limits)
    sub = {
        "apiVersion": "maas.opendatahub.io/v1alpha1",
        "kind": "MaaSSubscription",
        "metadata": {"name": args.name},
        "spec": {
            "owner": {"groups": groups, "users": users},
            "priority": int(args.priority),
            "modelRefs": [{"name": m, "namespace": args.namespace, "tokenRateLimits": limits} for m in models],
        },
    }
    policy = {
        "apiVersion": "maas.opendatahub.io/v1alpha1",
        "kind": "MaaSAuthPolicy",
        "metadata": {"name": args.name + "-access"},
        "spec": {
            "subjects": {"groups": [{"name": g["name"]} for g in groups], "users": users},
            "modelRefs": [{"name": m, "namespace": args.namespace} for m in models],
        },
    }
    return sub, policy


def subscription_set(objects, args):
    if not NAME_RE.match(args.name) or not NAME_RE.match(args.name + "-access"):
        fail("the subscription name must be a DNS label, and <name>-access must fit in 63 characters")
    sub, policy = subscription_objects(objects, args)
    names = {args.name, args.name + "-access"}
    objects = [o for o in objects
               if not (o.get("kind") in ("MaaSSubscription", "MaaSAuthPolicy")
                       and o.get("metadata", {}).get("name") in names)]
    return objects + [sub, policy]


def subscription_delete(objects, args):
    names = {args.name, args.name + "-access"}
    out = [o for o in objects
           if not (o.get("kind") in ("MaaSSubscription", "MaaSAuthPolicy")
                   and o.get("metadata", {}).get("name") in names)]
    if len(out) == len(objects):
        fail(f"no subscription {args.name}")
    return out


def group_edit(objects, args, add):
    sub = find(objects, "MaaSSubscription", args.subscription)
    if sub is None:
        fail(f"no subscription {args.subscription}")
    policy = find(objects, "MaaSAuthPolicy", args.subscription + "-access")
    if policy is None and add:
        policy = {
            "apiVersion": "maas.opendatahub.io/v1alpha1",
            "kind": "MaaSAuthPolicy",
            "metadata": {"name": args.subscription + "-access"},
            "spec": {
                "subjects": {"groups": [], "users": list((sub["spec"].get("owner") or {}).get("users") or [])},
                "modelRefs": [{"name": r["name"], "namespace": r.get("namespace", args.namespace)}
                              for r in (sub["spec"].get("modelRefs") or [])],
            },
        }
        objects = objects + [policy]
    for obj, holder_key in ((sub, "owner"), (policy, "subjects")):
        if obj is None:
            continue
        holder = obj["spec"].setdefault(holder_key, {})
        groups = [g["name"] for g in holder.get("groups") or []]
        if add and args.group not in groups:
            groups.append(args.group)
        if not add:
            groups = [g for g in groups if g != args.group]
        holder["groups"] = [{"name": g} for g in groups]
    return objects


def model_set(objects, args):
    if not NAME_RE.match(args.name):
        fail("the model name must be a DNS label")
    served = args.served_name or args.name
    objects = [o for o in objects
               if not (o.get("kind") == "MaaSModelRef" and o.get("metadata", {}).get("name") == args.name)]
    return objects + [{
        "apiVersion": "maas.opendatahub.io/v1alpha1",
        "kind": "MaaSModelRef",
        "metadata": {"name": args.name, "annotations": {"maas.moc/served-name": served}},
        "spec": {"modelRef": {"kind": "LLMInferenceService", "name": served}},
    }]


def model_delete(objects, args):
    refs = []
    for obj in objects:
        if obj.get("kind") in ("MaaSSubscription", "MaaSAuthPolicy"):
            for ref in (obj.get("spec") or {}).get("modelRefs") or []:
                if ref.get("name") == args.name:
                    refs.append(f"{obj['kind']}/{obj['metadata']['name']}")
    if refs:
        fail(f"model {args.name} is referenced by {', '.join(refs)}; delete those first")
    out = [o for o in objects
           if not (o.get("kind") == "MaaSModelRef" and o.get("metadata", {}).get("name") == args.name)]
    if len(out) == len(objects):
        fail(f"no model {args.name}")
    return out


def show(objects):
    summary = {"models": [], "subscriptions": [], "policies": []}
    for obj in objects:
        kind = obj.get("kind")
        spec = obj.get("spec") or {}
        if kind == "MaaSModelRef":
            summary["models"].append({
                "name": obj["metadata"]["name"],
                "served_name": (obj["metadata"].get("annotations") or {}).get("maas.moc/served-name", ""),
            })
        elif kind == "MaaSSubscription":
            summary["subscriptions"].append({
                "name": obj["metadata"]["name"],
                "priority": spec.get("priority", 0),
                "groups": [g["name"] for g in (spec.get("owner") or {}).get("groups") or []],
                "users": list((spec.get("owner") or {}).get("users") or []),
                "models": [{"name": r["name"],
                            "limits": [f"{l['limit']}/{l['window']}" for l in r.get("tokenRateLimits") or []]}
                           for r in spec.get("modelRefs") or []],
            })
        elif kind == "MaaSAuthPolicy":
            summary["policies"].append({
                "name": obj["metadata"]["name"],
                "groups": [g["name"] for g in (spec.get("subjects") or {}).get("groups") or []],
                "users": list((spec.get("subjects") or {}).get("users") or []),
                "models": [r["name"] for r in spec.get("modelRefs") or []],
            })
    print(json.dumps(summary, indent=2, sort_keys=True))
    return objects


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True)
    sub = p.add_subparsers(dest="action", required=True)

    def add(name, fn):
        s = sub.add_parser(name)
        s.set_defaults(fn=fn)
        return s

    s = add("subscription-set", subscription_set)
    s.add_argument("--name", required=True)
    s.add_argument("--namespace", required=True)
    s.add_argument("--groups", default="")
    s.add_argument("--users", default="")
    s.add_argument("--priority", default="0")
    s.add_argument("--models", default="")
    s.add_argument("--limits", required=True)

    s = add("subscription-delete", subscription_delete)
    s.add_argument("--name", required=True)

    s = add("group-map", lambda o, a: group_edit(o, a, True))
    s.add_argument("--subscription", required=True)
    s.add_argument("--group", required=True)
    s.add_argument("--namespace", required=True)

    s = add("group-unmap", lambda o, a: group_edit(o, a, False))
    s.add_argument("--subscription", required=True)
    s.add_argument("--group", required=True)
    s.add_argument("--namespace", required=True)

    s = add("model-set", model_set)
    s.add_argument("--name", required=True)
    s.add_argument("--served-name", default="")

    s = add("model-delete", model_delete)
    s.add_argument("--name", required=True)

    add("show", lambda o, a: show(o))

    args = p.parse_args()
    objects = load(args.file)
    save(args.file, args.fn(objects, args))


if __name__ == "__main__":
    main()
