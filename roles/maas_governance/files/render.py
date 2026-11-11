#!/usr/bin/env python3
# Assisted by Claude Opus
"""Render the governance data into the objects maas-controller and the
Kuadrant operator would generate."""

import argparse
import hashlib
import json
import os
import re
import sys

import yaml

KIND_RESOURCE = {
    "MaaSModelRef": "maasmodelref.maas.opendatahub.io",
    "MaaSSubscription": "maassubscription.maas.opendatahub.io",
    "MaaSAuthPolicy": "maasauthpolicy.maas.opendatahub.io",
}
NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
GROUP_RE = re.compile(r"^[a-zA-Z0-9:._-]+$")
WINDOW_RE = re.compile(r"^([1-9]\d{0,3})(s|m|h)$")
WINDOW_SECONDS = {"s": 1, "m": 60, "h": 3600}

CEL_USER_ID = (
    '(has(auth.metadata) && has(auth.metadata.apiKeyValidation)) '
    '? auth.metadata.apiKeyValidation.userId '
    ': (has(auth.identity.preferred_username) ? auth.identity.preferred_username '
    ': (has(auth.identity.sub) ? auth.identity.sub : auth.identity.user.username))'
)
CEL_USERNAME = (
    '(has(auth.metadata) && has(auth.metadata.apiKeyValidation)) '
    '? auth.metadata.apiKeyValidation.username '
    ': (has(auth.identity.preferred_username) ? auth.identity.preferred_username '
    ': (has(auth.identity.sub) ? auth.identity.sub : auth.identity.user.username))'
)
CEL_GROUPS = (
    '(has(auth.metadata) && has(auth.metadata.apiKeyValidation)) '
    '? auth.metadata.apiKeyValidation.groups '
    ': (has(auth.identity.groups) ? auth.identity.groups : auth.identity.user.groups)'
)
CEL_SAFE_GROUPS = "auth.identity.groups.all(g, g.matches('^[A-Za-z0-9:._/-]+$'))"
CEL_TOKEN_GROUPS_JSON = (
    "has(auth.identity.groups) ? "
    "(size(auth.identity.groups) > 0 && " + CEL_SAFE_GROUPS + " ? "
    "'[\"system:authenticated\",\"' + auth.identity.groups.join('\",\"') + '\"]' : "
    "'[\"system:authenticated\"]') : "
    "(has(auth.identity.user.groups) && size(auth.identity.user.groups) > 0 ? "
    "'[\"system:authenticated\",\"' + auth.identity.user.groups.join('\",\"') + '\"]' : "
    "'[\"system:authenticated\"]')"
)
CEL_SUBSCRIPTION = (
    '(has(auth.metadata) && has(auth.metadata.apiKeyValidation)) '
    '? auth.metadata.apiKeyValidation.subscription '
    ': ("x-maas-subscription" in request.headers ? request.headers["x-maas-subscription"] : "")'
)
PATH_PARTS = 'request.path.split("/").filter(x, x != "")'
PATH_MODEL_AVAILABLE = (
    "size(" + PATH_PARTS + ") >= 2 && " + PATH_PARTS + '[0] != "v1" && ' + PATH_PARTS + '[0] != "maas-api"'
    " && " + PATH_PARTS + '[0] != "maas-usage"'
)
CEL_MODEL_AVAILABLE = '(' + PATH_MODEL_AVAILABLE + ' || "x-gateway-model-name" in request.headers)'
CEL_MODEL_IDENTITY = (
    "(" + PATH_MODEL_AVAILABLE + " ? " + PATH_PARTS + '[0] + "/" + ' + PATH_PARTS + "[1]"
    + ' : ("x-gateway-model-name" in request.headers'
    + '   ? request.headers["x-gateway-model-name"]'
    + '   : ""))'
)
CEL_RESOLVED_MODEL = (
    '(has(auth.metadata["subscription-info"].resolvedModel) && '
    'auth.metadata["subscription-info"].resolvedModel != "" '
    '? auth.metadata["subscription-info"].resolvedModel : ' + CEL_MODEL_IDENTITY + ")"
)
CEL_IS_KEY = 'request.headers.authorization.matches("^Bearer sk-oai-.*")'
CEL_IS_NOT_KEY = '!request.headers.authorization.startsWith("Bearer sk-oai-")'
CEL_EXTRACT_KEY = 'request.headers.authorization.replace("Bearer ", "")'
CEL_JWT = 'request.headers.authorization.matches("^Bearer [^.]+\\\\.[^.]+\\\\.[^.]+$")'
AUTH_VALID_CACHE_KEY = '"api-key|" + (' + CEL_EXTRACT_KEY + ') + "|" + ' + CEL_MODEL_IDENTITY
GATEWAY_AUTHZ_CACHE_KEY = '(' + CEL_USER_ID + ') + "|" + (' + CEL_GROUPS + ').join(",") + "|" + ' + CEL_MODEL_IDENTITY
SUBSCRIPTION_CACHE_KEY = (
    '(' + CEL_USER_ID + ') + "|" + (' + CEL_GROUPS + ').join(",") + "|" + ('
    + CEL_SUBSCRIPTION + ') + "|" + ' + CEL_MODEL_IDENTITY
)
SUBSCRIPTION_BODY = (
    '{\n  "groups": ' + CEL_GROUPS + ',\n  "username": ' + CEL_USERNAME
    + ',\n  "requestedSubscription": ' + CEL_SUBSCRIPTION
    + ',\n  "requestedModel": ' + CEL_MODEL_IDENTITY + "\n}"
)

REGO_TENANT_STUB = """# Tenant hostname isolation stub.
# Replace with a real maas-api call to validate that the API key's tenant
# matches the gateway hostname (prevents Coke key on Pepsi gateway).
allow { true }"""

REGO_AUTH_VALID = """allow {
  object.get(input.auth.metadata, "apiKeyValidation", {})
  input.auth.metadata.apiKeyValidation.valid == true
}
allow {
  not input.auth.metadata.apiKeyValidation
}"""

REGO_SUBSCRIPTION_VALID = """allow {
	object.get(input.auth.metadata["subscription-info"], "name", "") != ""
	object.get(input.auth.metadata["subscription-info"], "error", "") == ""
	phase := object.get(input.auth.metadata["subscription-info"], "phase", "")
	any([phase == "Active", phase == "Degraded"])
	object.get(input.auth.metadata["subscription-info"], "deletionTimestamp", "") == ""
}"""

REGO_GROUPS_SAFE = """unsafe_group[g] {
	g := input.auth.identity.groups[_]
	not regex.match("^[A-Za-z0-9:._/-]+$", g)
}

allow {
	count(unsafe_group) == 0
}"""

REGO_MODEL_ACCESS = """
model_access := __MODEL_ACCESS__

request_path := object.get(input.context.request.http, "path", "")
request_headers := object.get(input.context.request.http, "headers", {})

path_parts := [p | p := split(request_path, "/")[_]; p != ""]

path_model_identity := sprintf("%s/%s", [path_parts[0], path_parts[1]]) {
	count(path_parts) >= 2
	path_parts[0] != "v1"
	path_parts[0] != "maas-api"
	path_parts[0] != "maas-usage"
}

header_model_identity := object.get(request_headers, "x-gateway-model-name", "")

model_identity := path_model_identity {
	path_model_identity != ""
} else := header_model_identity {
	header_model_identity != ""
} else := ""

username := input.auth.metadata.apiKeyValidation.username
	{ object.get(input.auth, "metadata", {}).apiKeyValidation.username != "" }
else := input.auth.identity.preferred_username
	{ object.get(input.auth, "identity", {}).preferred_username != "" }
else := input.auth.identity.sub
	{ object.get(input.auth, "identity", {}).sub != "" }
else := input.auth.identity.user.username
	{ object.get(input.auth, "identity", {}).user.username != "" }
else := ""

groups := input.auth.metadata.apiKeyValidation.groups
	{ object.get(input.auth, "metadata", {}).apiKeyValidation.groups != [] }
else := input.auth.identity.groups
	{ object.get(input.auth, "identity", {}).groups != [] }
else := input.auth.identity.user.groups
	{ object.get(input.auth, "identity", {}).user.groups != [] }
else := []

model_rules := object.get(model_access, model_identity, null)

# Management endpoints (e.g. /v1/models, /maas-api/v1/api-keys) carry no model context.
# Allow them here; subscription and rate-limit checks are gated by model-route conditions.
allow {
	model_identity == ""
}

# Inference path: deny by default when no MaaSAuthPolicy covers this model.
# Allow only when the caller's username or a group is explicitly listed.
allow {
	model_rules != null
	model_rules.users[_] == username
}

allow {
	model_rules != null
	g := groups[_]
	model_rules.groups[_] == g
}
"""


class Fail(Exception):
    pass


def fail(rule, obj, message):
    kind = obj.get("kind", "?")
    name = (obj.get("metadata") or {}).get("name", "?")
    raise Fail(f"rule {rule}: {kind}/{name}: {message}")


def unique_sorted(values):
    return sorted(set(values))


def parse_window(window):
    m = WINDOW_RE.match(str(window))
    if not m:
        return None
    return int(m.group(1)) * WINDOW_SECONDS[m.group(2)]


def check_objects(objects, namespace, admin_group):
    if not isinstance(objects, list) or not objects:
        raise Fail("rule 1: the governance data must be a non-empty list of objects")
    for obj in objects:
        if obj.get("apiVersion") != "maas.opendatahub.io/v1alpha1":
            fail(1, obj, "apiVersion must be maas.opendatahub.io/v1alpha1")
        if obj.get("kind") not in KIND_RESOURCE:
            fail(1, obj, "kind must be MaaSModelRef, MaaSSubscription or MaaSAuthPolicy")
        name = (obj.get("metadata") or {}).get("name", "")
        if not NAME_RE.match(name):
            fail(2, obj, "metadata.name must be a DNS label")
        if obj["kind"] == "MaaSModelRef":
            ann = (obj.get("metadata") or {}).get("annotations") or {}
            if not ann.get("maas.moc/served-name"):
                fail(8, obj, "missing the maas.moc/served-name annotation")

    models = {o["metadata"]["name"] for o in objects if o["kind"] == "MaaSModelRef"}
    for obj in objects:
        spec = obj.get("spec") or {}
        if obj["kind"] == "MaaSSubscription":
            for ref in spec.get("modelRefs") or []:
                if ref.get("namespace") != namespace:
                    fail(3, obj, f"modelRefs[].namespace must be {namespace}")
                if ref.get("name") not in models:
                    fail(3, obj, f"modelRefs[].name {ref.get('name')} is not a MaaSModelRef")
                for rate in ref.get("tokenRateLimits") or []:
                    if parse_window(rate.get("window")) is None:
                        fail(4, obj, f"window {rate.get('window')} must match ^[1-9]\\d{{0,3}}(s|m|h)$")
                    limit = rate.get("limit")
                    if not isinstance(limit, int) or not 1 <= limit <= 1000000000:
                        fail(4, obj, f"limit {limit} must be 1 to 1000000000")
            owner = spec.get("owner") or {}
            for group in owner.get("groups") or []:
                gname = group.get("name", "")
                if not GROUP_RE.match(gname):
                    fail(5, obj, f"group name {gname} must match ^[a-zA-Z0-9:._-]+$")
                if gname == admin_group:
                    fail(5, obj, f"group {gname} is reserved")
        if obj["kind"] == "MaaSAuthPolicy":
            for ref in spec.get("modelRefs") or []:
                if ref.get("namespace") != namespace:
                    fail(3, obj, f"modelRefs[].namespace must be {namespace}")
                if ref.get("name") not in models:
                    fail(3, obj, f"modelRefs[].name {ref.get('name')} is not a MaaSModelRef")
            for group in (spec.get("subjects") or {}).get("groups") or []:
                gname = group.get("name", "")
                if not GROUP_RE.match(gname):
                    fail(5, obj, f"group name {gname} must match ^[a-zA-Z0-9:._-]+$")
                if gname == admin_group:
                    fail(5, obj, f"group {gname} is reserved")

    priorities = [o.get("spec", {}).get("priority") for o in objects if o["kind"] == "MaaSSubscription"]
    if len(priorities) != len(set(priorities)):
        raise Fail("rule 6: MaaSSubscription priorities must be unique")

    sub_models = {
        ref.get("name")
        for o in objects
        if o["kind"] == "MaaSSubscription"
        for ref in (o.get("spec") or {}).get("modelRefs") or []
    }
    policy_models = {
        ref.get("name")
        for o in objects
        if o["kind"] == "MaaSAuthPolicy"
        for ref in (o.get("spec") or {}).get("modelRefs") or []
    }
    if sub_models != policy_models:
        raise Fail(
            "rule 7: every model a subscription references must be in a MaaSAuthPolicy and the other way round"
            f" (subscriptions: {sorted(sub_models)}, policies: {sorted(policy_models)})"
        )


def served_names(objects, namespace):
    return {
        o["metadata"]["name"]: o["metadata"]["annotations"]["maas.moc/served-name"]
        for o in objects
        if o["kind"] == "MaaSModelRef"
    }


def publisher_id(namespace, served):
    return f"publishers/{namespace}/models/{served}"


def model_access_json(objects, namespace, served):
    access = {}

    def entry(key):
        if key not in access:
            access[key] = {"users": set(), "groups": set()}
        return access[key]

    for obj in objects:
        if obj["kind"] != "MaaSAuthPolicy":
            continue
        spec = obj.get("spec") or {}
        subjects = spec.get("subjects") or {}
        for ref in spec.get("modelRefs") or []:
            key = f"{ref['namespace']}/{ref['name']}"
            e = entry(key)
            for g in subjects.get("groups") or []:
                e["groups"].add(g["name"])
            for u in subjects.get("users") or []:
                e["users"].add(u)
            alias = publisher_id(namespace, served[ref["name"]])
            a = entry(alias)
            a["groups"].update(e["groups"])
            a["users"].update(e["users"])
    out = {k: {"users": unique_sorted(v["users"]), "groups": unique_sorted(v["groups"])} for k, v in access.items()}
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


def token_limit_name(namespace, subscription, model):
    return f"{namespace}-{subscription}-{model}-tokens"


def limit_identifier(policy, limit_name):
    ident = "tokenlimit." + "".join(c if (c.isalnum() or c == "_") else "_" for c in limit_name)
    digest = hashlib.sha256(f"{policy}/{limit_name}".encode()).hexdigest()[:8]
    return ident + "__" + digest


def subscription_key(namespace, subscription, model):
    return f"{namespace}/{subscription}@{namespace}/{model}"


def build_limits(objects, namespace):
    models = [o["metadata"]["name"] for o in objects if o["kind"] == "MaaSModelRef"]
    subs = [o for o in objects if o["kind"] == "MaaSSubscription"]
    limits = []
    covered = set()
    for model in models:
        policy = f"{namespace}/maas-trlp-{model}"
        for sub in subs:
            refs = [r for r in (sub.get("spec") or {}).get("modelRefs") or [] if r.get("name") == model]
            if not refs:
                continue
            covered.add(model)
            sub_name = sub["metadata"]["name"]
            name = token_limit_name(namespace, sub_name, model)
            ident = limit_identifier(policy, name)
            for rate in refs[0].get("tokenRateLimits") or []:
                limits.append({
                    "name": name,
                    "namespace": f"{namespace}/{model}",
                    "max_value": rate["limit"],
                    "seconds": parse_window(rate["window"]),
                    "conditions": [f'descriptors[0]["{ident}"] == "1"'],
                    "variables": ['descriptors[0]["auth.identity.userid"]'],
                })
    for model in models:
        if model in covered:
            continue
        ident = limit_identifier(f"{namespace}/gateway-default-deny", "deny-all-by-default")
        limits.append({
            "name": "deny-all-by-default",
            "namespace": f"{namespace}/{model}",
            "max_value": 0,
            "seconds": 60,
            "conditions": [f'descriptors[0]["{ident}"] == "1"'],
            "variables": ['descriptors[0]["auth.identity.userid"]'],
        })
    return limits


# wasm-shim configuration for RHCL 1.5 / Kuadrant 1.6: every call is a typed
# gRPC action with a CEL message builder, and the services are all dynamic.
# wasm-shim 0.15 only parses a response body for a store action, so the report
# action reads the fields from the path that store fills.

RESPONSE_BODY_STORE_PATH = "kuadrant.internal.response.body"


def _cel_escape(s):
    return (str(s).replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))


CHECK_REQUEST_CEL = """envoy.service.auth.v3.CheckRequest {
  attributes: envoy.service.auth.v3.AttributeContext {
    request: envoy.service.auth.v3.AttributeContext.Request {
      time: request.time,
      http: envoy.service.auth.v3.AttributeContext.HttpRequest {
        host: request.host,
        method: request.method,
        scheme: request.scheme,
        path: request.path,
        protocol: request.protocol,
        headers: ("x-gateway-model-name" in request.headers)
          ? {
              "authorization": ("authorization" in request.headers) ? request.headers["authorization"] : "",
              "x-gateway-model-name": request.headers["x-gateway-model-name"],
              "x-maas-subscription": ("x-maas-subscription" in request.headers) ? request.headers["x-maas-subscription"] : ""
            }
          : {"authorization": ("authorization" in request.headers) ? request.headers["authorization"] : ""}
      }
    },
    destination: envoy.service.auth.v3.AttributeContext.Peer {
      address: envoy.config.core.v3.Address {
        socket_address: envoy.config.core.v3.SocketAddress {
          address: destination.address,
          port_value: uint(destination.port)
        }
      }
    },
    source: envoy.service.auth.v3.AttributeContext.Peer {
      address: envoy.config.core.v3.Address {
        socket_address: envoy.config.core.v3.SocketAddress {
          address: source.address,
          port_value: uint(source.port)
        }
      }
    },
    context_extensions: {"host": "__SCOPE__"},
    metadata_context: envoy.config.core.v3.Metadata{}
  }
}"""

TOKEN_DENY_BODY = r'"{\"error\": {\"message\": \"Too Many Requests\", \"type\": \"rate_limit_exceeded\", \"code\": 429}}"'
TOKEN_DENY_CONTENT_TYPE = '["content-type", "application/json"]'
METRIC_BINDINGS = [
    ("model", 'responseBodyJSON("/model")'),
    ("user", "auth.identity.userid"),
    ("subscription", "auth.identity.selected_subscription"),
]


def _entry(key, value_cel):
    return ('envoy.extensions.common.ratelimit.v3.RateLimitDescriptor.Entry '
            f'{{ key: "{_cel_escape(key)}", value: {value_cel} }}')


def _descriptor(entries_expr):
    return ("envoy.extensions.common.ratelimit.v3.RateLimitDescriptor "
            f"{{ entries: {entries_expr} }}")


def _rate_limit_request(scope, cds, bindings, hits_addend):
    unconditional, conditional = [], []
    for cd in cds:
        entries = ", ".join(_entry(k, v) for k, v in cd["entries"])
        preds = cd["predicates"]
        if not preds:
            unconditional.append(entries)
        else:
            pred = preds[0] if len(preds) == 1 else "(" + " && ".join(preds) + ")"
            conditional.append(f"(({pred}) ? [{entries}] : [])")
    entry_exprs = unconditional + conditional
    descriptors = [_descriptor(" + ".join(entry_exprs) if entry_exprs else "[]")]
    if bindings:
        descriptors.append(_descriptor("[" + ", ".join(
            _entry(k, f"string({v})") for k, v in bindings) + "]"))
    return ("envoy.service.ratelimit.v3.RateLimitRequest {\n"
            f'    domain: "{_cel_escape(scope)}",\n'
            f"    hits_addend: {hits_addend},\n"
            f"    descriptors: [{', '.join(descriptors)}]\n"
            "}")


def _descriptor_predicate(cds):
    if not cds or any(not cd["predicates"] for cd in cds):
        return "true"
    preds = [cd["predicates"][0] if len(cd["predicates"]) == 1
             else "(" + " && ".join(cd["predicates"]) + ")" for cd in cds]
    return preds[0] if len(preds) == 1 else " || ".join(preds)


def _grpc(predicate, var, service, message_builder, label, on_reply, sources=None,
          terminal=False, is_guard=True):
    action = {"type": "grpc", "predicate": predicate, "terminal": terminal,
              "isGuard": is_guard, "var": var, "service": service}
    if label:
        action["label"] = label
    action["messageBuilder"] = message_builder
    if on_reply:
        action["onReply"] = on_reply
    if sources:
        action["sources"] = sources
    return action


def _deny(predicate, deny_with):
    return {"type": "deny", "predicate": predicate, "terminal": True,
            "isGuard": True, "denyWith": deny_with}


def _headers(predicate, target, headers):
    return {"type": "headers", "predicate": predicate, "terminal": False,
            "isGuard": True, "target": target, "headers": headers}


def _store(predicate, path, value, export_to_host=False):
    action = {"type": "store", "predicate": predicate, "terminal": False,
              "isGuard": True, "path": path, "value": value}
    if export_to_host:
        action["exportToHost"] = True
    return action


def _fail(predicate, message, terminal=True, is_guard=True):
    return {"type": "fail", "predicate": predicate, "terminal": terminal,
            "isGuard": is_guard, "logMessage": message}


def _auth_action(scope, predicate):
    var = "auth_response"
    on_reply = [
        _deny(f"has({var}.denied_response)",
              "DenyResponse{status: (" + var + ".denied_response.status.code != 0) ? uint("
              + var + ".denied_response.status.code) : 403u, headers: " + var
              + ".denied_response.headers, body: " + var + ".denied_response.body}"),
        _fail(f"has({var}.ok_response) && ({var}.ok_response.response_headers_to_add.size() > 0"
              f" || {var}.ok_response.headers_to_remove.size() > 0"
              f" || {var}.ok_response.query_parameters_to_set.size() > 0"
              f" || {var}.ok_response.query_parameters_to_remove.size() > 0)",
              "Unsupported field in OkHttpResponse"),
        _store(f"has({var}.ok_response) && has({var}.dynamic_metadata)", "auth",
               f"{var}.dynamic_metadata", export_to_host=True),
        _headers(f"has({var}.ok_response)", "request", f"{var}.ok_response.headers"),
        _fail(f"!has({var}.denied_response) && !has({var}.ok_response)",
              f"Auth response contained no http_response from {var}"),
    ]
    builder = CHECK_REQUEST_CEL.replace("__SCOPE__", _cel_escape(scope))
    return _grpc(predicate, var, "auth-service", builder, "auth", on_reply)


def _check_action(scope, cds, sources):
    var = "ratelimit_response"
    on_reply = [
        _deny(f"{var}.overall_code == 2",
              f"DenyResponse{{status: 429u, headers: {var}.response_headers_to_add + ["
              f"{TOKEN_DENY_CONTENT_TYPE}], body: {TOKEN_DENY_BODY}}}"),
        _headers(f"{var}.overall_code == 1", "response", f"{var}.response_headers_to_add"),
        _fail(f"{var}.overall_code != 1 && {var}.overall_code != 2",
              f"Unknown rate limit response code from {var}"),
    ]
    # The check runs before the response body exists; bindings that read it
    # belong to the report action only.
    request_only = [(key, expr) for key, expr in METRIC_BINDINGS if "responseBodyJSON(" not in expr]
    builder = _rate_limit_request(scope, cds, request_only, "uint(0)")
    return _grpc(_descriptor_predicate(cds), var, "ratelimit-check-service", builder,
                 "ratelimit", on_reply, sources)


def _report_action(scope, cds, sources):
    var = "report_response"
    on_reply = [_fail(f"!has({var}.overall_code)",
                      "Rate limit report failed: invalid gRPC response",
                      terminal=False, is_guard=False)]
    builder = _rate_limit_request(scope, cds, METRIC_BINDINGS,
                                  'uint(responseBodyJSON("/usage/total_tokens"))')
    return _grpc(_descriptor_predicate(cds), var, "ratelimit-report-service", builder,
                 "ratelimit_report", on_reply, sources, is_guard=False)


def _response_body_store(builder):
    """Move the responseBodyJSON calls in a message builder into a store action.

    wasm-shim 0.15 no longer parses response bodies for arbitrary actions; a
    store action whose value reads the body owns the parser, and later actions
    read its map. Same layout Kuadrant 1.6 builds.
    """
    refs = sorted(set(re.findall(r'responseBodyJSON\("[^"]*"\)', builder)))
    if not refs:
        return None, builder
    pointers = {ref: ref[len("responseBodyJSON("):-1].strip('"') for ref in refs}
    leaf_counts = {}
    for pointer in pointers.values():
        leaf = _body_field_name(pointer)
        leaf_counts[leaf] = leaf_counts.get(leaf, 0) + 1
    used, names, entries = set(), {}, []
    for ref, pointer in pointers.items():
        name = _body_field_name(pointer)
        if leaf_counts[name] > 1:
            name = re.sub(r"[^A-Za-z0-9_]", "_", pointer)
        base, n = name, 2
        while name in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name)
        names[ref] = name
        entries.append(f'"{name}": {ref}')
    for ref, name in names.items():
        builder = builder.replace(ref, f"{RESPONSE_BODY_STORE_PATH}.{name}")
    action = _store("true", RESPONSE_BODY_STORE_PATH, "{" + ", ".join(entries) + "}")
    action["isGuard"] = False
    return action, builder


def _body_field_name(pointer):
    leaf = pointer.rstrip("/").rsplit("/", 1)[-1] or "value"
    return re.sub(r"[^A-Za-z0-9_]", "_", leaf)


def _token_cd(namespace, model, sub_name, policy, limit_name):
    ident = limit_identifier(policy, limit_name)
    condition = (
        f'auth.identity.selected_subscription_key == "{subscription_key(namespace, sub_name, model)}"'
        ' && !request.path.endsWith("/v1/models")'
    )
    return {"predicates": [condition],
            "entries": [(ident, "string(1)"), ("auth.identity.userid", "string(auth.identity.userid)")]}


def _deny_cd(namespace, model):
    policy = f"{namespace}/gateway-default-deny"
    ident = limit_identifier(policy, "deny-all-by-default")
    condition = (
        '!request.path.startsWith("/maas-api") && !request.path.startsWith("/v1/models")'
        ' && !request.path.startsWith("/v1/subscriptions") && !request.path.startsWith("/v1/api-keys")'
    )
    return {"predicates": [condition],
            "entries": [(ident, "string(1)"), ("auth.identity.userid", "string(auth.identity.userid)")]}


def _grpc_endpoint(cluster):
    """Envoy's wasm gRPC hostcall parses its upstream argument as a serialized
    GrpcService proto and only falls back to a cluster name when that fails.
    Some plain names (for example "authorino") are valid protobuf and would be
    misread as an empty service; send a real GrpcService{envoy_grpc} instead."""
    name = cluster.encode()
    envoy_grpc = b"\x0a" + bytes([len(name)]) + name
    return (b"\x0a" + bytes([len(envoy_grpc)]) + envoy_grpc).decode("latin-1")


def build_wasm_config(objects, namespace, served, authconfig_name, auth_timeout, ratelimit_timeout):
    subs = [o for o in objects if o["kind"] == "MaaSSubscription"]
    models = sorted(o["metadata"]["name"] for o in objects if o["kind"] == "MaaSModelRef")
    auth_predicate = 'request.path != "/maas-api/health" || request.method != "GET"'
    action_sets = [{
        "name": "maas-api",
        "routeRuleConditions": {
            "hostnames": ["*"],
            "predicates": [
                'request.url_path.startsWith("/v1/models") || request.url_path.startsWith("/v1/subscriptions")'
                ' || request.url_path.startsWith("/v1/api-keys") || request.url_path.startsWith("/maas-api/")'
                ' || request.url_path.startsWith("/maas-usage/")'
            ],
        },
        "actions": [_auth_action(authconfig_name, auth_predicate)],
    }]
    for model in models:
        cds, sources, covered = [], [], False
        for sub in subs:
            refs = [r for r in (sub.get("spec") or {}).get("modelRefs") or [] if r.get("name") == model]
            if not refs:
                continue
            covered = True
            sub_name = sub["metadata"]["name"]
            policy = f"{namespace}/maas-trlp-{model}"
            cds.append(_token_cd(namespace, model, sub_name, policy,
                                 token_limit_name(namespace, sub_name, model)))
            sources.append(f"kuadrant.io/v1alpha1/TokenRateLimitPolicy:{policy}")
        if not covered:
            cds.append(_deny_cd(namespace, model))
            sources.append(f"kuadrant.io/v1alpha1/TokenRateLimitPolicy:{namespace}/gateway-default-deny")
        sources = sorted(set(sources))
        report = _report_action(f"{namespace}/{model}", cds, sources)
        store, report["messageBuilder"] = _response_body_store(report["messageBuilder"])
        actions = [_auth_action(authconfig_name, auth_predicate),
                   _check_action(f"{namespace}/{model}", cds, sources), report]
        if store:
            store["sources"] = sources
            actions.insert(0, store)
        action_sets.append({
            "name": f"model-{model}",
            "routeRuleConditions": {
                "hostnames": ["*"],
                "predicates": [
                    '("x-gateway-model-name" in request.headers) && request.headers["x-gateway-model-name"] == "'
                    + publisher_id(namespace, served[model]) + '"'
                ],
            },
            "actions": actions,
        })
    return {
        "services": {
            "auth-service": {"type": "dynamic", "endpoint": _grpc_endpoint("authorino"), "failureMode": "deny",
                             "timeout": auth_timeout,
                             "grpcService": "envoy.service.auth.v3.Authorization", "grpcMethod": "Check"},
            "ratelimit-check-service": {"type": "dynamic", "endpoint": _grpc_endpoint("limitador"), "failureMode": "allow",
                                        "timeout": ratelimit_timeout,
                                        "grpcService": "kuadrant.service.ratelimit.v1.RateLimitService",
                                        "grpcMethod": "CheckRateLimit"},
            "ratelimit-report-service": {"type": "dynamic", "endpoint": _grpc_endpoint("limitador"), "failureMode": "allow",
                                         "timeout": ratelimit_timeout,
                                         "grpcService": "kuadrant.service.ratelimit.v1.RateLimitService",
                                         "grpcMethod": "Report"},
        },
        "actionSets": action_sets,
    }


def build_authconfig(objects, namespace, args, served):
    access = model_access_json(objects, namespace, served)
    issuer = f"https://{args.sso_host}/realms/maas"
    return {
        "apiVersion": "authorino.kuadrant.io/v1beta3",
        "kind": "AuthConfig",
        "metadata": {
            "name": args.authconfig_name,
            "namespace": namespace,
            "labels": {"maas.moc/managed": "true"},
        },
        "spec": {
            "hosts": [args.authconfig_name],
            "authentication": {
                "api-keys": {
                    "plain": {"selector": "request.headers.authorization"},
                    "when": [{"selector": "request.headers.authorization", "operator": "matches", "value": "^Bearer sk-oai-.*"}],
                    "metrics": False,
                    "priority": 0,
                },
                "oidc-identities": {
                    "jwt": {"issuerUrl": issuer, "ttl": 300},
                    "when": [{"predicate": CEL_IS_NOT_KEY + " && " + CEL_JWT}],
                    "metrics": False,
                    "priority": 1,
                },
            },
            "metadata": {
                "apiKeyValidation": {
                    "when": [{"predicate": CEL_IS_KEY}],
                    "http": {
                        "url": f"http://127.0.0.1:{args.api_port}/internal/v1/api-keys/validate",
                        "contentType": "application/json",
                        "method": "POST",
                        "body": {"expression": '{"key": ' + CEL_EXTRACT_KEY + "}"},
                    },
                    "cache": {"key": {"selector": CEL_EXTRACT_KEY}, "ttl": args.cache_ttl},
                    "metrics": False,
                    "priority": 0,
                },
                "subscription-info": {
                    "when": [{"predicate": CEL_MODEL_AVAILABLE}],
                    "http": {
                        "url": f"http://127.0.0.1:{args.api_port}/internal/v1/subscriptions/select",
                        "contentType": "application/json",
                        "method": "POST",
                        "body": {"expression": SUBSCRIPTION_BODY},
                    },
                    "cache": {"key": {"selector": SUBSCRIPTION_CACHE_KEY}, "ttl": args.cache_ttl},
                    "metrics": False,
                    "priority": 1,
                },
            },
            "authorization": {
                "tenant-gateway-isolation": {
                    "priority": 0,
                    "metrics": False,
                    "opa": {"rego": REGO_TENANT_STUB, "version": "v0"},
                },
                "auth-valid": {
                    "metrics": False,
                    "priority": 0,
                    "opa": {"rego": REGO_AUTH_VALID, "version": "v0"},
                    "cache": {"key": {"selector": AUTH_VALID_CACHE_KEY}, "ttl": args.cache_ttl},
                },
                "subscription-valid": {
                    "when": [{"predicate": CEL_MODEL_AVAILABLE}],
                    "metrics": False,
                    "priority": 0,
                    "opa": {"rego": REGO_SUBSCRIPTION_VALID, "version": "v0"},
                    "cache": {"key": {"selector": SUBSCRIPTION_CACHE_KEY}, "ttl": args.cache_ttl},
                },
                "require-group-membership": {
                    "metrics": False,
                    "priority": 0,
                    "opa": {"rego": REGO_MODEL_ACCESS.replace("__MODEL_ACCESS__", access), "version": "v0"},
                    "cache": {"key": {"selector": GATEWAY_AUTHZ_CACHE_KEY}, "ttl": args.cache_ttl},
                },
                "oidc-groups-safe": {
                    "when": [{"predicate": CEL_IS_NOT_KEY + " && has(auth.identity.groups) && size(auth.identity.groups) > 0"}],
                    "metrics": False,
                    "priority": 0,
                    "opa": {"rego": REGO_GROUPS_SAFE, "version": "v0"},
                },
                "oidc-client-bound": {
                    "when": [{"predicate": CEL_IS_NOT_KEY + " && " + CEL_JWT + " && has(auth.identity.azp)"}],
                    "metrics": False,
                    "priority": 1,
                    "patternMatching": {"patterns": [{"selector": "auth.identity.azp", "operator": "eq", "value": args.oidc_client}]},
                },
                "no-reserved-admin-group": {
                    "metrics": False,
                    "priority": 0,
                    "patternMatching": {
                        "patterns": [{
                            "predicate": f'!(has(auth.identity.groups) && auth.identity.groups.exists(g, g == "{args.admin_group}"))',
                        }],
                    },
                },
            },
            "response": {
                "success": {
                    "headers": {
                        "X-MaaS-Username": {
                            "when": [{"predicate": CEL_IS_KEY}],
                            "plain": {"selector": "auth.metadata.apiKeyValidation.username"},
                            "metrics": False,
                            "priority": 0,
                        },
                        "X-MaaS-Username-Token": {
                            "when": [{"predicate": CEL_IS_NOT_KEY}],
                            "plain": {
                                "expression": "has(auth.identity.preferred_username) ? auth.identity.preferred_username : (has(auth.identity.sub) ? auth.identity.sub : auth.identity.user.username)"
                            },
                            "key": "X-MaaS-Username",
                            "metrics": False,
                            "priority": 1,
                        },
                        # A logging-only copy of the user: the payload
                        # post-stage strips x-maas-* before the access log.
                        "X-Auth-User": {
                            "when": [{"predicate": CEL_IS_KEY}],
                            "plain": {"selector": "auth.metadata.apiKeyValidation.username"},
                            "metrics": False,
                            "priority": 0,
                        },
                        "X-Auth-User-Token": {
                            "when": [{"predicate": CEL_IS_NOT_KEY}],
                            "plain": {
                                "expression": "has(auth.identity.preferred_username) ? auth.identity.preferred_username : (has(auth.identity.sub) ? auth.identity.sub : auth.identity.user.username)"
                            },
                            "key": "X-Auth-User",
                            "metrics": False,
                            "priority": 1,
                        },
                        "X-MaaS-Group": {
                            "when": [{"predicate": CEL_IS_KEY}],
                            "plain": {
                                "expression": "size(auth.metadata.apiKeyValidation.groups) > 0 ? '[\"' + auth.metadata.apiKeyValidation.groups.join('\",\"') + '\"]' : '[]'"
                            },
                            "metrics": False,
                            "priority": 0,
                        },
                        "X-MaaS-Group-Token": {
                            "when": [{"predicate": CEL_IS_NOT_KEY}],
                            "plain": {"expression": CEL_TOKEN_GROUPS_JSON},
                            "key": "X-MaaS-Group",
                            "metrics": False,
                            "priority": 1,
                        },
                        "X-MaaS-Subscription": {
                            "when": [{
                                "predicate": '(has(auth.metadata) && has(auth.metadata.apiKeyValidation) && auth.metadata.apiKeyValidation.subscription != "") || "x-maas-subscription" in request.headers'
                            }],
                            "plain": {"expression": CEL_SUBSCRIPTION},
                            "metrics": False,
                            "priority": 0,
                        },
                    },
                    "dynamicMetadata": {
                        # Same guard as subscription-info: without it, /maas-api
                        # requests fail here and Authorino drops other headers.
                        "identity": {
                            "when": [{"predicate": CEL_MODEL_AVAILABLE}],
                            "json": {
                                "properties": {
                                    "groups": {"expression": CEL_GROUPS},
                                    "groups_str": {"expression": '(' + CEL_GROUPS + ').join(",")'},
                                    "userid": {"expression": CEL_USERNAME},
                                    "keyId": {"expression": '(has(auth.metadata) && has(auth.metadata.apiKeyValidation)) ? auth.metadata.apiKeyValidation.keyId : ""'},
                                    "keyName": {"expression": '(has(auth.metadata) && has(auth.metadata.apiKeyValidation)) ? auth.metadata.apiKeyValidation.keyName : ""'},
                                    "selected_subscription": {"expression": 'has(auth.metadata["subscription-info"].name) ? auth.metadata["subscription-info"].name : ""'},
                                    "selected_subscription_key": {
                                        "expression": '(has(auth.metadata["subscription-info"].namespace) && has(auth.metadata["subscription-info"].name)) ? auth.metadata["subscription-info"].namespace + "/" + auth.metadata["subscription-info"].name + "@" + ' + CEL_RESOLVED_MODEL + ' : ""'
                                    },
                                    "subscription_info": {"expression": 'has(auth.metadata["subscription-info"].name) ? auth.metadata["subscription-info"] : {}'},
                                    "subscription_error": {"expression": 'has(auth.metadata["subscription-info"].error) ? auth.metadata["subscription-info"].error : ""'},
                                    "subscription_error_message": {"expression": 'has(auth.metadata["subscription-info"].message) ? auth.metadata["subscription-info"].message : ""'},
                                },
                            },
                            "metrics": True,
                            "priority": 0,
                        },
                    },
                },
                "unauthenticated": {"code": 401, "message": {"value": "Authentication required"}},
                "unauthorized": {
                    "code": 403,
                    "body": {"expression": 'has(auth.metadata["subscription-info"].message) ? auth.metadata["subscription-info"].message : "Access denied"'},
                    "headers": {
                        "x-ext-auth-reason": {"expression": 'has(auth.metadata["subscription-info"].error) ? auth.metadata["subscription-info"].error : "unauthorized"'},
                        "content-type": {"value": "text/plain"},
                    },
                },
            },
        },
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--objects", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--gateway-host", required=True)
    p.add_argument("--sso-host", required=True)
    p.add_argument("--oidc-client", required=True)
    p.add_argument("--cache-ttl", type=int, required=True)
    p.add_argument("--admin-group", required=True)
    p.add_argument("--authconfig-name", required=True)
    p.add_argument("--api-port", type=int, required=True)
    p.add_argument("--auth-timeout", required=True)
    p.add_argument("--ratelimit-timeout", required=True)
    args = p.parse_args()

    with open(args.objects) as f:
        objects = yaml.safe_load(f)

    try:
        check_objects(objects, args.namespace, args.admin_group)
    except Fail as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    served = served_names(objects, args.namespace)
    os.makedirs(args.out, exist_ok=True)

    seed_objects = []
    for obj in objects:
        o = json.loads(json.dumps(obj))
        md = o.setdefault("metadata", {})
        md["namespace"] = args.namespace
        labels = md.setdefault("labels", {})
        labels["maas.moc/managed"] = "true"
        seed_objects.append(o)

    status_lines = []
    for obj in objects:
        kind = obj["kind"]
        name = obj["metadata"]["name"]
        res = KIND_RESOURCE[kind]
        if kind == "MaaSModelRef":
            status = {
                "phase": "Ready",
                "resolvedModelAlias": publisher_id(args.namespace, served[name]),
                "endpoint": f"https://{args.gateway_host}",
            }
        else:
            status = {"phase": "Active"}
        status_lines.append(f"{res} {name} {json.dumps({'status': status}, separators=(',', ':'))}")

    names_lines = [f"{KIND_RESOURCE[o['kind']]}/{o['metadata']['name']}" for o in objects]
    names_lines.append(f"authconfig.authorino.kuadrant.io/{args.authconfig_name}")

    with open(os.path.join(args.out, "objects.yaml"), "w") as f:
        yaml.safe_dump_all(seed_objects, f, sort_keys=False, width=1000, explicit_start=True)
    with open(os.path.join(args.out, "status.txt"), "w") as f:
        f.write("\n".join(status_lines) + "\n")
    with open(os.path.join(args.out, "names.txt"), "w") as f:
        f.write("\n".join(names_lines) + "\n")
    with open(os.path.join(args.out, "authconfig.yaml"), "w") as f:
        yaml.safe_dump(build_authconfig(objects, args.namespace, args, served), f, sort_keys=False, width=100000)
    with open(os.path.join(args.out, "limits.yaml"), "w") as f:
        yaml.safe_dump(build_limits(objects, args.namespace), f, sort_keys=False, width=100000)
    with open(os.path.join(args.out, "wasm-config.json"), "w") as f:
        f.write(json.dumps(build_wasm_config(objects, args.namespace, served, args.authconfig_name,
                                             args.auth_timeout, args.ratelimit_timeout),
                           separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
