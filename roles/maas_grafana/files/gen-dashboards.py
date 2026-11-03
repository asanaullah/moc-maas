#!/usr/bin/env python3
"""Steps 23-24: renders the admin Grafana's two dashboards as a ConfigMap
Grafana provisions from at startup, to stdout; deploy.yml applies it:

  MaaS usage    tokens, requests and 429s per person and per application
                (service key), share of budget, key inventory
  MaaS serving  replicas, load balancing across them, queues, latency, prefix
                cache, gateway and key-check outcomes

Edit this file, then re-run playbooks/deploy.yml (or just its Grafana play) to apply.
Usage: gen-dashboards.py [namespace]
"""
import json
import sys

# The namespace the ConfigMap lands in; the Grafana play passes it in.
NS = sys.argv[1] if len(sys.argv) > 1 else "uat-project"

DS = {"type": "prometheus", "uid": "maas-thanos"}
_id = [0]


def panel(kind, title, targets, x, y, w, h, unit=None, desc=None, **extra):
    _id[0] += 1
    p = {"id": _id[0], "type": kind, "title": title, "datasource": DS,
         "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [{"refId": chr(65 + i), "datasource": DS, "expr": e, "legendFormat": lf,
                      **({"format": "table", "instant": True} if kind == "table" else {})}
                     for i, (e, lf) in enumerate(targets)],
         "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []}}
    if desc:
        p["description"] = desc
    p.update(extra)
    return p


def dashboard(uid, title, panels, desc):
    return {"uid": uid, "title": title, "description": desc, "editable": False, "schemaVersion": 39,
            "time": {"from": "now-6h", "to": "now"}, "refresh": "30s", "tags": ["maas"],
            "panels": panels, "templating": {"list": []}}


PEOPLE = 'user!~"service:.*"'
APPS = 'user=~"service:.*"'

usage = dashboard("maas-usage", "MaaS usage", [
    panel("stat", "Tokens, last 24h", [("sum(increase(authorized_hits[24h]))", "")], 0, 0, 6, 4, "short"),
    panel("stat", "Requests counted, last 24h", [("sum(increase(report_calls[24h]))", "")], 6, 0, 6, 4, "short"),
    panel("stat", "Refused for budget (429), last 24h", [("sum(increase(limited_calls[24h]))", "")], 12, 0, 6, 4, "short"),
    panel("stat", "Active keys", [('sum by (kind) (maas_keys{status="active"})', "{{kind}}")], 18, 0, 6, 4, "short"),
    panel("timeseries", "People: tokens per minute", [(f"sum by (user) (rate(authorized_hits{{{PEOPLE}}}[5m])) * 60", "{{user}}")],
          0, 4, 12, 8, "short", "Users' own keys. Counted from each response's token usage."),
    panel("timeseries", "Applications (service keys): tokens per minute",
          [(f"sum by (user) (rate(authorized_hits{{{APPS}}}[5m])) * 60", "{{user}}")], 12, 4, 12, 8, "short"),
    panel("timeseries", "Refused for budget (429) per minute, by owner",
          [("sum by (user) (rate(limited_calls[5m])) * 60", "{{user}}")], 0, 12, 12, 7, "short"),
    panel("timeseries", "Share of budget, 5-minute average",
          [("sum by (user, plan) (rate(authorized_hits[5m])) / on (plan) group_left "
            "max by (plan) (maas_plan_budget_tokens / maas_plan_window_seconds)", "{{user}} ({{plan}})")],
          12, 12, 12, 7, "percentunit", "Token rate as a share of the plan's budget rate. Above 100% means the owner is being refused."),
    panel("table", "Tokens per owner and plan, last 24h",
          [("sort_desc(sum by (user, plan) (increase(authorized_hits[24h])))", "")], 0, 19, 12, 9, "short",
          transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                           "renameByName": {"user": "owner", "Value": "tokens"}}}]),
    panel("table", "Keys", [("maas_key_info", "")], 12, 19, 12, 9, None,
          "Every key, with its owner, kind (user or service), plan in force and status.",
          transformations=[{"id": "organize", "options": {"excludeByName": {
              "Time": True, "Value": True, "__name__": True, "instance": True, "job": True, "cluster": True,
              "namespace": True, "prometheus": True, "key_id": True}}}]),
    panel("table", "Plans", [("maas_plan_budget_tokens", "")], 0, 28, 12, 6, "short",
          transformations=[{"id": "organize", "options": {"excludeByName": {
              "Time": True, "__name__": True, "instance": True, "job": True, "cluster": True, "namespace": True,
              "prometheus": True}, "renameByName": {"Value": "tokens per window"}}}]),
    panel("timeseries", "Key checks by result", [("sum by (result) (rate(maas_key_checks_total[5m])) * 60", "{{result}}")],
          12, 28, 12, 6, "short", "ok, invalid (bad, expired or revoked key), missing (no key), model_denied (model not in plan)."),
], "Usage per person and per application, against their plans' budgets.")

_id[0] = 0
serving = dashboard("maas-serving", "MaaS serving", [
    panel("stat", "Replicas ready in the pool", [("max(llm_d_epp_ready_endpoints)", "")], 0, 0, 6, 4, "short"),
    panel("stat", "Requests per second", [('sum(rate(envoy_http_downstream_rq_total{envoy_http_conn_manager_prefix="maas"}[5m]))', "")],
          6, 0, 6, 4, "reqps"),
    panel("stat", "Median time to first token", [("histogram_quantile(0.5, sum by (le) (rate(llm_d_epp_request_ttft_seconds_bucket[5m])))", "")],
          12, 0, 6, 4, "s"),
    panel("stat", "Gateway 5xx share", [('sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="maas",envoy_response_code_class="5"}[5m])) '
                                         '/ sum(rate(envoy_http_downstream_rq_total{envoy_http_conn_manager_prefix="maas"}[5m]))', "")],
          18, 0, 6, 4, "percentunit"),
    panel("timeseries", "Requests per replica (load balancing)",
          [("sum by (endpoint_name) (rate(llm_d_epp_scheduler_attempts_total[5m]))", "{{endpoint_name}}")], 0, 4, 12, 8, "reqps",
          "Where the endpoint picker sent requests."),
    panel("timeseries", "Queue per replica", [("max by (model_server_endpoint) (llm_d_epp_per_endpoint_queue_size)", "{{model_server_endpoint}}")],
          12, 4, 12, 8, "short"),
    panel("timeseries", "Running and waiting requests per model pod",
          [("max by (instance) (vllm:num_requests_running)", "running {{instance}}"),
           ("max by (instance) (vllm:num_requests_waiting)", "waiting {{instance}}")], 0, 12, 12, 8, "short"),
    panel("timeseries", "Time to first token", [
        ("histogram_quantile(0.5, sum by (le) (rate(llm_d_epp_request_ttft_seconds_bucket[5m])))", "p50"),
        ("histogram_quantile(0.95, sum by (le) (rate(llm_d_epp_request_ttft_seconds_bucket[5m])))", "p95")], 12, 12, 12, 8, "s"),
    panel("timeseries", "Median time to first token, per owner",
          [("histogram_quantile(0.5, sum by (le, fairness_id) (rate(llm_d_epp_request_ttft_seconds_bucket[5m])))", "{{fairness_id}}")],
          0, 20, 12, 8, "s", "By the key's owner (the picker's fairness ID)."),
    panel("timeseries", "Prefix-cache hit ratio (median)",
          [("histogram_quantile(0.5, sum by (le) (rate(llm_d_epp_prefix_indexer_hit_ratio_bucket[5m])))", "hit ratio")],
          12, 20, 12, 8, "percentunit", "How much of each prompt the chosen replica already had cached; chat traffic sticks to one replica for this."),
    panel("timeseries", "Gateway responses by status class",
          [('sum by (envoy_response_code_class) (rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix="maas"}[5m]))',
            "{{envoy_response_code_class}}xx")], 0, 28, 12, 7, "reqps"),
    panel("timeseries", "Pool balance (standard deviation across replicas)",
          [("max(llm_d_epp_std_dev_queue_size)", "queue"), ("max(llm_d_epp_std_dev_running_requests)", "running")],
          12, 28, 12, 7, "short", "Zero means perfectly even load."),
], "Replicas, how the picker spreads load across them, and latency.")

body = "".join(
    f"  {name}.json: |\n" + "".join(f"    {line}\n" for line in json.dumps(d, indent=1).splitlines())
    for name, d in (("maas-usage", usage), ("maas-serving", serving)))
print(
    "# Step 24: the admin Grafana's dashboards. Generated by gen-dashboards.py at\n"
    "# deploy time; edit that rather than editing this by hand.\n"
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: maas-grafana-dashboards\n  namespace: "
    + NS + "\n"
    "  labels:\n    app.kubernetes.io/part-of: maas-test\ndata:\n" + body)
