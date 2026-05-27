#!/usr/bin/env python3
"""
Azure App Service Plans – Cost & Rightsizing Exporter
=====================================================
Exports App Service Plans together with hosted-app inventory and real Azure
Monitor metrics so you can identify empty plans, low-utilisation plans,
oversized SKUs, scaling pressure, workload mix, and plan density.

Output: semicolon-delimited, BOM-prefixed CSV (Excel-compatible).

What this collects
------------------
- Plan inventory: SKU, tier, capacity, OS kind, zone redundancy, scaling flags
- Hosted workload mix: web apps, Function Apps, Logic Apps Standard
- App posture hints: running/stopped state, HTTPS-only, Always On, runtime stack
- Plan metrics: CPU %, Memory %, HTTP queue, Disk queue, bytes in/out
- Plan metrics: CPU %, Memory %, instance count, HTTP queue, Disk queue, bytes in/out
- Derived cost signals: empty plan, stopped-only plan, likely scale-in candidate,
  likely oversized premium plan, or scale-up review candidate

Prerequisites
-------------
    pip install azure-identity azure-mgmt-web azure-mgmt-monitor

Authentication
--------------
    Same as the other extractors in this repo: reads the customer CSV (-i),
    groups subscriptions by tenant, runs `az login --tenant` once per tenant
    (unless --skip-login is set), then iterates all matching subscriptions.

Usage
-----
    python get_appserviceplans.py -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    python get_appserviceplans.py -i ../customers/CUST.csv --lookback PT168H --output-dir ./reports/CUST
    python get_appserviceplans.py -s <subscription-id> --from 2026-01-01 --to 2026-03-31 --output-dir ./reports
"""

import argparse
import csv
import json
import os
import requests
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


# ── dependency check ─────────────────────────────────────────────────────────

_REQUIRED = {
    "azure.identity": "azure-identity",
    "azure.mgmt.web": "azure-mgmt-web",
    "azure.mgmt.monitor": "azure-mgmt-monitor",
    "requests": "requests",
}

_missing = []
for mod, pkg in _REQUIRED.items():
    try:
        __import__(mod)
    except ImportError:
        _missing.append(pkg)

if _missing:
    print("ERROR: The following packages could not be imported:\n")
    for pkg in _missing:
        print(f"    ✗  {pkg}")
    print(f"\nPython executable : {sys.executable}")
    print(f"Python version    : {sys.version}")
    print("\nInstall into the same Python that runs this script:")
    print(f"\n    {sys.executable} -m pip install {' '.join(_missing)}\n")
    sys.exit(1)

from azure.identity import (
    AzureCliCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
)
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.web import WebSiteManagementClient


# ── Summary file: one row per plan ────────────────────────────────────────
SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "name",
    "plan_name",
    "plan_id",
    "location",
    "kind",
    "status",
    "os_type",
    "hosting_environment_name",
    "sku_tier",
    "sku_name",
    "sku_size",
    "sku_family",
    "sku_capacity",
    "maximum_number_of_workers",
    "maximum_elastic_worker_count",
    "target_worker_count",
    "target_worker_size_id",
    "per_site_scaling",
    "elastic_scale_enabled",
    "zone_redundant",
    "is_spot",
    "reserved",
    "hyper_v",
    "tags",
    "apps_count",
    "running_apps_count",
    "stopped_apps_count",
    "functionapps_count",
    "webapps_count",
    "logicapps_count",
    "always_on_apps_count",
    "https_only_apps_count",
    "client_affinity_enabled_count",
    "primary_workload_kind",
    "workload_mix",
    "runtime_summary",
    "hosted_app_names",
    "hosted_apps_json",
    "apps_per_worker",
    "cost_signal_category",
    "cost_signal_reason",
    "observed_sample_count",
    "observed_instance_avg_window",
    "observed_instance_peak_window",
    "cpu_avg_pct_window",
    "cpu_p95_pct_window",
    "cpu_peak_pct_window",
    "memory_avg_pct_window",
    "memory_p95_pct_window",
    "memory_peak_pct_window",
    "http_queue_avg_window",
    "http_queue_peak_window",
    "disk_queue_avg_window",
    "disk_queue_peak_window",
    "bytes_received_total_window",
    "bytes_sent_total_window",
    "qc_not_free_shared",
    "qc_min_2_instances",
    "qc_zone_redundant",
    "qc_has_apps",
    "qc_has_tags",
]

# ── Timeseries file: one row per plan × timestamp ──────────────────────────
TIMESERIES_COLUMNS = [
    "plan_id",
    "timestamp",
    "instance_count",
    "cpu_percentage",
    "memory_percentage",
    "http_queue_length",
    "disk_queue_length",
    "bytes_received",
    "bytes_sent",
]


PLAN_METRICS = [
    {"azure_name": "InstanceCount", "column": "instance_count", "aggregation": "Average"},
    {"azure_name": "CpuPercentage", "column": "cpu_percentage", "aggregation": "Average"},
    {"azure_name": "MemoryPercentage", "column": "memory_percentage", "aggregation": "Average"},
    {"azure_name": "HttpQueueLength", "column": "http_queue_length", "aggregation": "Average"},
    {"azure_name": "DiskQueueLength", "column": "disk_queue_length", "aggregation": "Average"},
    {"azure_name": "BytesReceived", "column": "bytes_received", "aggregation": "Total"},
    {"azure_name": "BytesSent", "column": "bytes_sent", "aggregation": "Total"},
]


def parse_lookback(value: str) -> timedelta:
    try:
        return timedelta(minutes=int(value))
    except ValueError:
        pass
    import re

    match = re.match(r"PT(?:(\d+)D)?(?:(\d+)H)?(?:(\d+)M)?", value, re.IGNORECASE)
    if match:
        return timedelta(
            days=int(match.group(1) or 0),
            hours=int(match.group(2) or 0),
            minutes=int(match.group(3) or 0),
        )
    raise argparse.ArgumentTypeError(f"Cannot parse lookback '{value}'")


def parse_date(value: str) -> datetime:
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Cannot parse date '{value}'. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."
    )


def read_customer_csv(json_path: str) -> dict:
    if not os.path.exists(json_path):
        print(f"ERROR: Customer file not found: {json_path}")
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    tenant_map = defaultdict(list)
    for entry in data.get("azure", []):
        status = str(entry.get("status") or "").strip().lower()
        if status != "active":
            continue
        tenant = (entry.get("tenant_id") or "").strip()
        sub_id = (entry.get("subscription_id") or "").strip()
        sub_name = (entry.get("subscription_name") or "").strip()
        if tenant and sub_id:
            tenant_map[tenant].append((sub_id, sub_name))

    if not tenant_map:
        print(f"ERROR: No subscriptions found in {json_path}")
        sys.exit(1)

    total = sum(len(v) for v in tenant_map.values())
    print(f"Input '{json_path}': {total} subscription(s) across {len(tenant_map)} tenant(s)")
    return dict(tenant_map)


def get_credential(
    tenant_id: str | None = None,
    sp_client_id: str | None = None,
    sp_client_secret: str | None = None,
    sp_certificate: str | None = None,
):
    chain_candidates = []
    resolved_tenant = tenant_id or os.environ.get("AZURE_TENANT_ID")
    if sp_client_id:
        if not resolved_tenant:
            raise ValueError("Service principal auth requires tenant_id or AZURE_TENANT_ID.")
        if sp_certificate:
            chain_candidates.append(
                CertificateCredential(
                    tenant_id=resolved_tenant,
                    client_id=sp_client_id,
                    certificate_path=sp_certificate,
                )
            )
        else:
            if not sp_client_secret:
                raise ValueError("Provide --sp-client-secret or --sp-certificate with --sp-client-id.")
            chain_candidates.append(
                ClientSecretCredential(
                    tenant_id=resolved_tenant,
                    client_id=sp_client_id,
                    client_secret=sp_client_secret,
                )
            )

    default_cred = DefaultAzureCredential(additionally_allowed_tenants=["*"])
    chain_candidates.append(default_cred)
    if tenant_id:
        chain_candidates.append(AzureCliCredential(tenant_id=tenant_id))
    else:
        chain_candidates.append(AzureCliCredential())
    return ChainedTokenCredential(*chain_candidates)


def get_token(credential) -> str:
    return credential.get_token("https://management.azure.com/.default").token


def list_enabled_subscriptions(credential) -> list[tuple[str, str, str]]:
    token = get_token(credential)
    headers = {"Authorization": f"Bearer {token}"}
    url = "https://management.azure.com/subscriptions?api-version=2022-12-01"
    subscriptions: list[tuple[str, str, str]] = []

    while url:
        response = requests.get(url, headers=headers, timeout=120)
        response.raise_for_status()
        payload = response.json()
        for sub in payload.get("value", []):
            state = str(sub.get("state") or "")
            if state.lower().endswith("enabled"):
                subscriptions.append(
                    (
                        str(sub.get("subscriptionId") or ""),
                        str(sub.get("displayName") or sub.get("subscriptionId") or ""),
                        str(sub.get("tenantId") or ""),
                    )
                )
        url = payload.get("nextLink")

    return subscriptions


def fmt(value):
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def parse_resource_group(resource_id: str) -> str:
    if not resource_id or "/resourceGroups/" not in resource_id:
        return ""
    return resource_id.split("/resourceGroups/")[1].split("/")[0]


def tags_str(tags: dict | None) -> str:
    if not tags:
        return ""
    return ", ".join(f"{k}={v}" for k, v in sorted(tags.items()))


def number_to_str(value, decimals: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        rounded = round(value, decimals)
        if rounded.is_integer():
            return str(int(rounded))
        return f"{rounded:.{decimals}f}"
    return str(value)


def percentile(values: list[float], pct: float) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    rank = (len(clean) - 1) * (pct / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(clean) - 1)
    weight = rank - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def detect_app_category(kind: str) -> str:
    kind_lower = (kind or "").lower()
    if "functionapp" in kind_lower:
        return "functionapp"
    if "workflow" in kind_lower or "logicapp" in kind_lower:
        return "logicapp"
    return "webapp"


def detect_runtime_stack(config) -> str:
    if not config:
        return ""

    linux_fx = getattr(config, "linux_fx_version", None)
    windows_fx = getattr(config, "windows_fx_version", None)
    if linux_fx:
        return str(linux_fx)
    if windows_fx:
        return str(windows_fx)

    candidates = [
        ("python", getattr(config, "python_version", None)),
        ("node", getattr(config, "node_version", None)),
        ("php", getattr(config, "php_version", None)),
        ("dotnet", getattr(config, "net_framework_version", None)),
        ("java", getattr(config, "java_version", None)),
        ("powershell", getattr(config, "power_shell_version", None)),
    ]
    for label, value in candidates:
        if value:
            return f"{label}:{value}"
    return ""


def summarize_apps(apps: list[dict], sku_capacity: int | None) -> dict:
    counts = {
        "webapp": 0,
        "functionapp": 0,
        "logicapp": 0,
        "running": 0,
        "stopped": 0,
        "always_on": 0,
        "https_only": 0,
        "client_affinity": 0,
    }
    runtime_set = set()
    names = []

    compact_apps = []
    for app in sorted(apps, key=lambda item: item.get("name", "")):
        category = app.get("category", "webapp")
        counts[category] = counts.get(category, 0) + 1

        state = (app.get("state") or "").lower()
        if state == "running":
            counts["running"] += 1
        elif state:
            counts["stopped"] += 1

        if app.get("always_on") is True:
            counts["always_on"] += 1
        if app.get("https_only") is True:
            counts["https_only"] += 1
        if app.get("client_affinity_enabled") is True:
            counts["client_affinity"] += 1

        if app.get("runtime_stack"):
            runtime_set.add(app["runtime_stack"])
        if app.get("name"):
            names.append(app["name"])

        compact_apps.append(
            {
                "name": app.get("name", ""),
                "category": category,
                "state": app.get("state", ""),
                "runtime": app.get("runtime_stack", ""),
                "always_on": app.get("always_on"),
                "https_only": app.get("https_only"),
            }
        )

    workload_counts = [
        ("webapp", counts["webapp"]),
        ("functionapp", counts["functionapp"]),
        ("logicapp", counts["logicapp"]),
    ]
    primary_workload = max(workload_counts, key=lambda item: item[1])[0] if apps else ""
    if apps and max(item[1] for item in workload_counts) == 0:
        primary_workload = ""

    capacity = sku_capacity or 0
    apps_per_worker = (len(apps) / capacity) if capacity > 0 else None

    return {
        "apps_count": len(apps),
        "running_apps_count": counts["running"],
        "stopped_apps_count": counts["stopped"],
        "functionapps_count": counts["functionapp"],
        "webapps_count": counts["webapp"],
        "logicapps_count": counts["logicapp"],
        "always_on_apps_count": counts["always_on"],
        "https_only_apps_count": counts["https_only"],
        "client_affinity_enabled_count": counts["client_affinity"],
        "primary_workload_kind": primary_workload,
        "workload_mix": ", ".join(f"{k}:{v}" for k, v in workload_counts if v),
        "runtime_summary": ", ".join(sorted(runtime_set)),
        "hosted_app_names": " | ".join(names),
        "hosted_apps_json": json.dumps(compact_apps, separators=(",", ":")),
        "apps_per_worker": apps_per_worker,
    }


def summarize_metric_values(values: list[float], metric_name: str) -> dict:
    if not values:
        return {"samples": 0, "avg": None, "p95": None, "peak": None, "total": None}

    summary = {
        "samples": len(values),
        "avg": sum(values) / len(values),
        "p95": percentile(values, 95),
        "peak": max(values),
        "total": None,
    }
    if metric_name in {"bytes_received", "bytes_sent"}:
        summary["total"] = sum(values)
    return summary


def build_cost_signal(plan_info: dict, app_summary: dict, metric_summaries: dict) -> tuple[str, str]:
    apps_count = app_summary["apps_count"]
    running_apps_count = app_summary["running_apps_count"]
    sku_tier = (plan_info.get("sku_tier") or "").lower()
    sku_capacity = plan_info.get("sku_capacity") or 0

    cpu_p95 = metric_summaries["cpu_percentage"]["p95"]
    cpu_peak = metric_summaries["cpu_percentage"]["peak"]
    mem_p95 = metric_summaries["memory_percentage"]["p95"]
    mem_peak = metric_summaries["memory_percentage"]["peak"]
    http_peak = metric_summaries["http_queue_length"]["peak"]

    premium_like = {
        "premium",
        "premiumv2",
        "premiumv3",
        "premiumv4",
        "isolated",
        "isolatedv2",
        "workflowstandard",
    }

    if apps_count == 0:
        return "orphaned", "Plan hosts no apps and is a direct hosting-cost savings candidate."

    if apps_count > 0 and running_apps_count == 0:
        return "stopped-only", "Plan hosts apps but none are currently running. Review whether the plan is still needed."

    if (
        sku_tier in premium_like
        and apps_count <= 2
        and (cpu_p95 is None or cpu_p95 < 20)
        and (mem_p95 is None or mem_p95 < 35)
        and (http_peak is None or http_peak < 1)
    ):
        return "oversized-premium", "Premium or isolated SKU with low sustained utilisation and low app density."

    if (
        sku_capacity >= 2
        and (cpu_p95 is None or cpu_p95 < 25)
        and (mem_p95 is None or mem_p95 < 35)
        and (http_peak is None or http_peak < 1)
    ):
        return "scale-in-candidate", "Multiple workers provisioned but low plan utilisation suggests scale-in review."

    if (
        (cpu_peak is not None and cpu_peak >= 85)
        or (mem_peak is not None and mem_peak >= 85)
        or (http_peak is not None and http_peak >= 10)
    ):
        return "scale-up-review", "Observed high peak utilisation or request queueing. Review SKU or worker count."

    return "review", "Plan has hosted workloads and no clear red flag; use workload mix and trend data for manual review."


def get_metric_series(
    monitor_client: MonitorManagementClient,
    resource_id: str,
    metric_name: str,
    timespan: str,
    aggregation: str,
    interval: str,
) -> list[tuple[str, float | None]]:
    try:
        result = monitor_client.metrics.list(
            resource_uri=resource_id,
            metricnames=metric_name,
            aggregation=aggregation,
            timespan=timespan,
            interval=interval,
        )
    except Exception as exc:
        print(f"    ⚠  Metric query failed ({metric_name}): {exc}")
        return []

    output = []
    property_name = aggregation.lower()
    for metric in result.value:
        for timeseries in metric.timeseries:
            for point in timeseries.data:
                timestamp = point.time_stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if point.time_stamp else ""
                value = getattr(point, property_name, None)
                if value is None:
                    continue
                output.append((timestamp, float(value)))
    return output


def fetch_plan_metric_series(
    monitor_client: MonitorManagementClient,
    plan_id: str,
    timespan: str,
    interval: str,
) -> dict[str, list[tuple[str, float | None]]]:
    series_by_column = {}
    for metric in PLAN_METRICS:
        series_by_column[metric["column"]] = get_metric_series(
            monitor_client,
            plan_id,
            metric["azure_name"],
            timespan,
            metric["aggregation"],
            interval,
        )
    return series_by_column


def safe_get_app_configuration(web_client: WebSiteManagementClient, resource_group: str, app_name: str):
    try:
        return web_client.web_apps.get_configuration(resource_group, app_name)
    except Exception:
        return None


def build_plan_app_inventory(web_client: WebSiteManagementClient) -> dict[str, list[dict]]:
    inventory = defaultdict(list)
    try:
        all_apps = list(web_client.web_apps.list())
    except Exception as exc:
        print(f"  ⚠  Could not list web apps: {exc}")
        return inventory

    print(f"  Hosted apps discovered: {len(all_apps)}")

    for app in all_apps:
        plan_id = (app.server_farm_id or "").lower()
        if not plan_id:
            continue

        resource_group = parse_resource_group(app.id or "")
        config = safe_get_app_configuration(web_client, resource_group, app.name)
        kind = fmt(app.kind)

        inventory[plan_id].append(
            {
                "name": fmt(app.name),
                "resource_group": resource_group,
                "kind": kind,
                "category": detect_app_category(kind),
                "state": fmt(getattr(app, "state", None)),
                "usage_state": fmt(getattr(app, "usage_state", None)),
                "https_only": getattr(app, "https_only", None),
                "client_affinity_enabled": getattr(app, "client_affinity_enabled", None),
                "always_on": getattr(config, "always_on", None) if config else None,
                "runtime_stack": detect_runtime_stack(config),
            }
        )

    return inventory


def process_subscription(
    credential,
    sub_id: str,
    sub_name: str,
    tenant_id: str,
    timespan: str,
    interval: str,
) -> tuple[list[dict], list[dict]]:
    print(f"\n── Subscription: {sub_name or sub_id} ──")
    web_client = WebSiteManagementClient(credential, sub_id)
    monitor_client = MonitorManagementClient(credential, sub_id)
    summary_rows: list[dict] = []
    ts_rows: list[dict] = []

    plan_apps = build_plan_app_inventory(web_client)

    try:
        plans = list(web_client.app_service_plans.list())
    except Exception as exc:
        print(f"  ⚠  Could not list App Service Plans: {exc}")
        return summary_rows, ts_rows

    if not plans:
        print("  (no App Service Plans found)")
        return summary_rows, ts_rows

    print(f"  Found {len(plans)} App Service Plan(s)")

    for plan in plans:
        plan_id = fmt(plan.id)
        plan_key = plan_id.lower()
        resource_group = parse_resource_group(plan_id)
        sku = getattr(plan, "sku", None)
        app_summary = summarize_apps(plan_apps.get(plan_key, []), getattr(sku, "capacity", None))

        hosting_env = getattr(plan, "hosting_environment_profile", None)
        hosting_environment_name = fmt(getattr(hosting_env, "name", None))
        os_type = "Linux" if getattr(plan, "reserved", False) else "Windows"

        plan_info = {
            "tenant_id": tenant_id or "",
            "subscription_id": sub_id,
            "subscription_name": sub_name or "",
            "resource_group": resource_group,
            "plan_name": fmt(plan.name),
            "plan_id": plan_id,
            "location": fmt(plan.location),
            "kind": fmt(plan.kind),
            "status": fmt(getattr(plan, "status", None)),
            "os_type": os_type,
            "hosting_environment_name": hosting_environment_name,
            "sku_tier": fmt(getattr(sku, "tier", None)),
            "sku_name": fmt(getattr(sku, "name", None)),
            "sku_size": fmt(getattr(sku, "size", None)),
            "sku_family": fmt(getattr(sku, "family", None)),
            "sku_capacity": getattr(sku, "capacity", None) if sku else None,
            "maximum_number_of_workers": getattr(plan, "maximum_number_of_workers", None),
            "maximum_elastic_worker_count": getattr(plan, "maximum_elastic_worker_count", None),
            "target_worker_count": getattr(plan, "target_worker_count", None),
            "target_worker_size_id": getattr(plan, "target_worker_size_id", None),
            "per_site_scaling": getattr(plan, "per_site_scaling", None),
            "elastic_scale_enabled": getattr(plan, "elastic_scale_enabled", None),
            "zone_redundant": getattr(plan, "zone_redundant", None),
            "is_spot": getattr(plan, "is_spot", None),
            "reserved": getattr(plan, "reserved", None),
            "hyper_v": getattr(plan, "hyper_v", None),
            "tags": tags_str(getattr(plan, "tags", None)),
        }

        print(
            f"\n  Plan: {plan_info['plan_name']}  (RG: {resource_group})  "
            f"SKU: {plan_info['sku_tier'] or 'unknown'} / {plan_info['sku_name'] or 'unknown'}  "
            f"Apps: {app_summary['apps_count']}"
        )

        print("    Fetching plan metrics ...", end=" ", flush=True)
        metric_series = fetch_plan_metric_series(monitor_client, plan_id, timespan, interval)
        ts_set = sorted(set(ts for series in metric_series.values() for ts, _ in series))
        print(f"{len(ts_set)} time points")

        metric_summaries = {}
        for column, series in metric_series.items():
            values = [value for _, value in series if value is not None]
            metric_summaries[column] = summarize_metric_values(values, column)

        cost_signal_category, cost_signal_reason = build_cost_signal(plan_info, app_summary, metric_summaries)

        sku_tier_lower = (plan_info.get("sku_tier") or "").strip().lower()
        non_paid_tiers = {"free", "shared", "dynamic"}
        qc_not_free_shared = sku_tier_lower not in non_paid_tiers
        qc_min_2_instances = (plan_info.get("sku_capacity") or 0) >= 2
        qc_zone_redundant = bool(plan_info.get("zone_redundant"))
        qc_has_apps = app_summary.get("apps_count", 0) > 0
        qc_has_tags = bool(plan_info.get("tags"))

        base_row = {
            **plan_info,
            **app_summary,
            "name": plan_info.get("plan_name", ""),
            "cost_signal_category": cost_signal_category,
            "cost_signal_reason": cost_signal_reason,
            "observed_sample_count": max((summary["samples"] for summary in metric_summaries.values()), default=0),
            "observed_instance_avg_window": metric_summaries["instance_count"]["avg"],
            "observed_instance_peak_window": metric_summaries["instance_count"]["peak"],
            "cpu_avg_pct_window": metric_summaries["cpu_percentage"]["avg"],
            "cpu_p95_pct_window": metric_summaries["cpu_percentage"]["p95"],
            "cpu_peak_pct_window": metric_summaries["cpu_percentage"]["peak"],
            "memory_avg_pct_window": metric_summaries["memory_percentage"]["avg"],
            "memory_p95_pct_window": metric_summaries["memory_percentage"]["p95"],
            "memory_peak_pct_window": metric_summaries["memory_percentage"]["peak"],
            "http_queue_avg_window": metric_summaries["http_queue_length"]["avg"],
            "http_queue_peak_window": metric_summaries["http_queue_length"]["peak"],
            "disk_queue_avg_window": metric_summaries["disk_queue_length"]["avg"],
            "disk_queue_peak_window": metric_summaries["disk_queue_length"]["peak"],
            "bytes_received_total_window": metric_summaries["bytes_received"]["total"],
            "bytes_sent_total_window": metric_summaries["bytes_sent"]["total"],
            "qc_not_free_shared": qc_not_free_shared,
            "qc_min_2_instances": qc_min_2_instances,
            "qc_zone_redundant": qc_zone_redundant,
            "qc_has_apps": qc_has_apps,
            "qc_has_tags": qc_has_tags,
        }

        ts_map = {ts: {} for ts in ts_set}
        for column, series in metric_series.items():
            for ts, value in series:
                if ts in ts_map:
                    ts_map[ts][column] = value

        # Summary row — one per plan
        summary_rows.append({key: number_to_str(base_row.get(key)) for key in SUMMARY_COLUMNS})

        # Timeseries rows — one per timestamp
        for ts in ts_set:
            values = ts_map[ts]
            ts_row = {
                "plan_id": plan_id,
                "timestamp": ts,
                "instance_count": values.get("instance_count"),
                "cpu_percentage": values.get("cpu_percentage"),
                "memory_percentage": values.get("memory_percentage"),
                "http_queue_length": values.get("http_queue_length"),
                "disk_queue_length": values.get("disk_queue_length"),
                "bytes_received": values.get("bytes_received"),
                "bytes_sent": values.get("bytes_sent"),
            }
            ts_rows.append({key: number_to_str(ts_row[key]) for key in TIMESERIES_COLUMNS})

    return summary_rows, ts_rows


def export(args):
    now = datetime.now(timezone.utc)
    fmt_iso = "%Y-%m-%dT%H:%M:%SZ"

    if args.date_from:
        start = parse_date(args.date_from)
        end = parse_date(args.date_to) if args.date_to else now
        if start >= end:
            print(f"ERROR: --from ({args.date_from}) must be before --to ({args.date_to or 'now'})")
            sys.exit(1)
        print(f"📅  Date range: {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        lookback = parse_lookback(args.lookback or "PT168H")
        start = now - lookback
        end = now
        print(f"📅  Lookback: last {lookback}  ({start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC)")

    timespan = f"{start.strftime(fmt_iso)}/{end.strftime(fmt_iso)}"
    all_summary: list[dict] = []
    all_ts: list[dict] = []

    if args.input:
        tenant_map = read_customer_csv(args.input)
        for tenant_id, subs in tenant_map.items():
            try:
                credential = get_credential(
                    tenant_id,
                    args.sp_client_id,
                    args.sp_client_secret,
                    args.sp_certificate,
                )
            except Exception as exc:
                print(f"  Skipping tenant {tenant_id} (credential error: {exc})")
                continue
            for sub_id, sub_name in subs:
                s, t = process_subscription(
                    credential, sub_id, sub_name, tenant_id, timespan, args.interval
                )
                all_summary.extend(s)
                all_ts.extend(t)
    elif args.subscription:
        credential = get_credential()
        s, t = process_subscription(
            credential, args.subscription, "", "", timespan, args.interval
        )
        all_summary.extend(s)
        all_ts.extend(t)
    else:
        credential = get_credential(None, args.sp_client_id, args.sp_client_secret, args.sp_certificate)
        subs = list_enabled_subscriptions(credential)
        print(f"Found {len(subs)} enabled subscription(s) via ARM SDK")
        for sub_id, sub_name, tenant_id in subs:
            s, t = process_subscription(
                credential, sub_id, sub_name, tenant_id, timespan, args.interval
            )
            all_summary.extend(s)
            all_ts.extend(t)

    os.makedirs(args.output_dir, exist_ok=True)
    date_range = f"{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}"

    if args.input:
        prefix = os.path.splitext(os.path.basename(args.input))[0].upper()
    else:
        prefix = "app_service_plans"
        date_range = date_range  # no prefix change needed

    summary_filename = f"{prefix}_app_service_plans_summary_{date_range}.csv" if args.input else f"app_service_plans_summary_{date_range}.csv"
    ts_filename = f"{prefix}_app_service_plans_timeseries_{date_range}.csv" if args.input else f"app_service_plans_timeseries_{date_range}.csv"

    summary_path = os.path.join(args.output_dir, summary_filename)
    ts_path = os.path.join(args.output_dir, ts_filename)
    summary_json_path = os.path.join(args.output_dir, summary_filename.replace(".csv", ".json"))
    ts_json_path = os.path.join(args.output_dir, ts_filename.replace(".csv", ".json"))

    if args.output_format in ("csv", "both"):
        with open(summary_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(all_summary)

        with open(ts_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TIMESERIES_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(all_ts)

    if args.output_format in ("json", "both"):
        with open(summary_json_path, "w", encoding="utf-8") as handle:
            json.dump(all_summary, handle, indent=2)

        with open(ts_json_path, "w", encoding="utf-8") as handle:
            json.dump(all_ts, handle, indent=2)

    unique_plans = len(all_summary)
    unique_named_apps = set()
    for row in all_summary:
        names = (row.get("hosted_app_names") or "").split(" | ")
        for name in names:
            if name:
                unique_named_apps.add((row.get("plan_name", ""), name))

    print(f"\n{'═' * 70}")
    print(f"  ✅  Exported {unique_plans} plan(s), {len(unique_named_apps)} hosted app(s), {len(all_ts)} timeseries row(s)")
    if args.output_format in ("csv", "both"):
        print(f"      → {summary_path}")
        print(f"      → {ts_path}")
    if args.output_format in ("json", "both"):
        print(f"      → {summary_json_path}")
        print(f"      → {ts_json_path}")
    print(f"{'═' * 70}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Export Azure App Service Plan hosting-cost and rightsizing data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
    %(prog)s -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.csv --lookback PT168H --interval PT1H --output-dir ./reports/CUST
    %(prog)s -s <subscription-id> --from 2026-01-01 --to 2026-03-31 --output-dir ./reports
        """,
    )

    parser.add_argument(
        "-i", "--input",
        default=None,
        help="Path to semicolon-delimited customer CSV (all rows processed)",
    )
    parser.add_argument(
        "-s", "--subscription",
        help="Single subscription ID (alternative to -i mode)",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Deprecated no-op; kept for backward compatibility",
    )
    parser.add_argument(
        "--sp-client-id",
        default=None,
        metavar="APP_ID",
        help="App Registration client ID for non-interactive service principal login.",
    )
    parser.add_argument(
        "--sp-client-secret",
        default=os.environ.get("AZURE_SP_CLIENT_SECRET"),
        metavar="SECRET",
        help="Client secret for service principal login. "
             "Falls back to AZURE_SP_CLIENT_SECRET env var.",
    )
    parser.add_argument(
        "--sp-certificate",
        default=None,
        metavar="CERT_PATH",
        help="Path to PEM certificate for service principal auth "
             "(alternative to --sp-client-secret).",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Start date YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="End date YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (defaults to now)",
    )
    parser.add_argument(
        "--lookback",
        default="PT168H",
        help="Lookback window when --from is omitted. Integer minutes or ISO-8601 duration (default: PT168H)",
    )
    parser.add_argument(
        "--interval",
        default="PT1H",
        help="Azure Monitor metric interval (default: PT1H)",
    )
    parser.add_argument(
        "--output-format",
        choices=("csv", "json", "both"),
        default="both",
        help="File output format: csv, json, or both (default: both)",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for the output files (default: current directory). Created if it doesn't exist.",
    )

    args = parser.parse_args()
    export(args)


if __name__ == "__main__":
    main()