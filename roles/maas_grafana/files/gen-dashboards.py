#!/usr/bin/env python3
# Assisted by Claude Opus
"""Renders the admin Grafana's two dashboards as a ConfigMap
Grafana provisions from at startup, to stdout; deploy.yml applies it:

  MaaS usage    tokens, requests and 429s per user, subscription and model
  MaaS serving  replicas, load balancing across them, queues, latency, prefix
                cache, and gateway outcomes

Edit this file, then re-run playbooks/deploy.yml (or just its Grafana play) to apply.
Usage: gen-dashboards.py [namespace]
"""
import json
import sys

# The namespace the ConfigMap lands in; the Grafana play passes it in.
NS = sys.argv[1] if len(sys.argv) > 1 else "uat-project"

DS = {"type": "prometheus", "uid": "maas-thanos"}
_id = [0]


def counter_total(metric, window="24h"):
    """Counter change over the window, including a series' first sample when
    the series was created inside it; increase() alone misses that value."""
    return (f"sum(increase({metric}[{window}]))"
            f" + sum(min_over_time({metric}[{window}]) unless {metric} offset {window})")


def counter_total_by(metric, labels, window="24h"):
    by = ", ".join(labels)
    return (f"sum by ({by}) (increase({metric}[{window}]))"
            f" + sum by ({by}) (min_over_time({metric}[{window}]) unless {metric} offset {window})")


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


usage = dashboard("maas-usage", "MaaS usage", [
    panel("stat", "Tokens, last 24h", [(counter_total("authorized_hits"), "")], 0, 0, 8, 4, "short"),
    panel("stat", "Requests counted, last 24h", [(counter_total("report_calls"), "")], 8, 0, 8, 4, "short"),
    panel("stat", "Refused for budget (429), last 24h", [(counter_total("limited_calls"), "")], 16, 0, 8, 4, "short"),
    panel("timeseries", "Tokens per minute by user",
          [("sum by (user) (rate(authorized_hits[5m])) * 60", "{{user}}")], 0, 4, 12, 8, "short",
          "Users' own keys; each response's token usage."),
    panel("timeseries", "Tokens per minute by subscription",
          [("sum by (subscription) (rate(authorized_hits[5m])) * 60", "{{subscription}}")], 12, 4, 12, 8, "short"),
    panel("timeseries", "Tokens per minute by model",
          [("sum by (model) (rate(authorized_hits[5m])) * 60", "{{model}}")], 0, 12, 12, 8, "short"),
    panel("timeseries", "Refused for budget (429) per minute, by user",
          [("sum by (user) (rate(limited_calls[5m])) * 60", "{{user}}")], 12, 12, 12, 8, "short"),
    panel("table", "Tokens per user and subscription, last 24h",
          [(f"sort_desc({counter_total_by('authorized_hits', ['user', 'subscription'])})", "")], 0, 20, 12, 9, "short",
          transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                           "renameByName": {"Value": "tokens"}}}]),
    panel("table", "Tokens per model, last 24h",
          [(f"sort_desc({counter_total_by('authorized_hits', ['model'])})", "")], 12, 20, 12, 9, "short",
          transformations=[{"id": "organize", "options": {"excludeByName": {"Time": True},
                                                           "renameByName": {"Value": "tokens"}}}]),
], "Usage per user, subscription and model.")

_id[0] = 0
serving = dashboard("maas-serving", "MaaS serving", [
    panel("stat", "Replicas ready in the pool", [("max(llm_d_epp_ready_endpoints)", "")], 0, 0, 6, 4, "short"),
    panel("stat", "Requests per second", [('sum(rate(envoy_http_downstream_rq_total{envoy_http_conn_manager_prefix=~"maas|maas-tls"}[5m]))', "")],
          6, 0, 6, 4, "reqps"),
    panel("stat", "Median time to first token", [("histogram_quantile(0.5, sum by (le) (rate(llm_d_epp_request_ttft_seconds_bucket[5m])))", "")],
          12, 0, 6, 4, "s"),
    panel("stat", "Gateway 5xx share", [(('sum(rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix=~"maas|maas-tls",envoy_response_code_class="5"}[5m])) '
                                          '/ sum(rate(envoy_http_downstream_rq_total{envoy_http_conn_manager_prefix=~"maas|maas-tls"}[5m]))'), "")],
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
    panel("timeseries", "Prefix-cache hit ratio (median)",
          [("histogram_quantile(0.5, sum by (le) (rate(llm_d_epp_prefix_indexer_hit_ratio_bucket[5m])))", "hit ratio")],
          0, 20, 12, 8, "percentunit", "How much of each prompt the chosen replica already had cached; chat traffic sticks to one replica for this."),
    panel("timeseries", "Pool balance (standard deviation across replicas)",
          [("max(llm_d_epp_std_dev_queue_size)", "queue"), ("max(llm_d_epp_std_dev_running_requests)", "running")],
          12, 20, 12, 8, "short", "Zero means perfectly even load."),
    panel("timeseries", "Gateway responses by status class",
          [('sum by (envoy_response_code_class) (rate(envoy_http_downstream_rq_xx{envoy_http_conn_manager_prefix=~"maas|maas-tls"}[5m]))',
            "{{envoy_response_code_class}}xx")], 0, 28, 12, 7, "reqps"),
    panel("timeseries", "Upstream connection failures",
          [("sum by (envoy_cluster_name) (rate(envoy_cluster_upstream_cx_connect_fail{envoy_cluster_name=~\"authorino|limitador|ipp_pre|ipp_post|epp|model-.*\"}[5m]))",
            "{{envoy_cluster_name}}")], 12, 28, 12, 7, "reqps", "Should stay at zero; a rising line means a gateway dependency is refusing connections."),
    {"id": 100, "type": "row", "title": "Health", "collapsed": False, "gridPos": {"x": 0, "y": 36, "w": 24, "h": 1},
     "panels": []},
    panel("stat", "Storage volume free", [("min(maas_volume_free_bytes / maas_volume_size_bytes)", "")],
          0, 37, 8, 4, "percentunit", "Below 15% needs attention: the databases and backups share this volume."),
    panel("table", "Firing alerts", [('ALERTS{alertstate="firing"}', "")], 8, 37, 16, 8, None,
          "Alerts the MaaS Prometheus evaluates (manifests/prometheus.yaml, alerts.yml).",
          transformations=[{"id": "organize", "options": {"excludeByName": {
              "Time": True, "Value": True, "__name__": True, "alertstate": True, "cluster": True,
              "prometheus": True}}}]),
], "Replicas, how the picker spreads load across them, and latency.")

body = "".join(
    f"  {name}.json: |\n" + "".join(f"    {line}\n" for line in json.dumps(d, indent=1).splitlines())
    for name, d in (("maas-usage", usage), ("maas-serving", serving)))
print(
    "# The admin Grafana's dashboards. Generated by gen-dashboards.py at\n"
    "# deploy time; edit that rather than editing this by hand.\n"
    "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: maas-grafana-dashboards\n  namespace: "
    + NS + "\n"
    "  labels:\n    app.kubernetes.io/part-of: maas-test\ndata:\n" + body)
