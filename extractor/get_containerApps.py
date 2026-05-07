#!/usr/bin/env python3
"""
Azure Container Apps – Utilisation Exporter
============================================
Exports workload profiles, container apps/jobs, and real CPU/Memory
utilisation metrics from Azure Monitor into a CSV file that can
be fed into the companion HTML dashboard.

Output: semicolon-delimited, BOM-prefixed CSV (Excel-compatible).

Prerequisites
-------------
    pip install azure-identity azure-mgmt-appcontainers azure-mgmt-monitor

Authentication
--------------
    Same as get_monthly_costs: reads the customer CSV (-i), groups
    subscriptions by tenant, runs `az login --tenant` once per tenant
    (unless --skip-login is set), then iterates all matching subscriptions.

Usage
-----
    python get_containerApps.py -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    python get_containerApps.py -i ../customers/CUST.csv --lookback PT6H --output-dir ./reports/CUST
    python get_containerApps.py -s <subscription-id> --output-dir ./reports
"""

import argparse
import csv
import io
import json
import os
import requests
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


# ── dependency check with clear diagnostics ──────────────────────────

_REQUIRED = {
    "azure.identity": "azure-identity",
    "azure.mgmt.appcontainers": "azure-mgmt-appcontainers",
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
    print(f"ERROR: The following packages could not be imported:\n")
    for pkg in _missing:
        print(f"    ✗  {pkg}")
    print(f"\nPython executable : {sys.executable}")
    print(f"Python version    : {sys.version}")
    print(f"\nMake sure you install into the SAME Python that runs this script:")
    print(f"\n    {sys.executable} -m pip install {' '.join(_missing)}\n")
    sys.exit(1)

from azure.identity import (
    AzureCliCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
)
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.monitor import MonitorManagementClient


# ── CSV columns ──────────────────────────────────────────────────────

# Summary file: one row per container
SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "environment_name",
    "app_name",
    "workload_kind",
    "container_name",
    "is_sidecar",
    "resource_id",
    "location",
    "workload_profile_name",
    "workload_profile_type",
    "workload_sku_cpu_per_instance",
    "workload_sku_memory_mib_per_instance",
    "profile_capacity_cpu_total",
    "profile_capacity_memory_mib_total",
    "profile_allocated_cpu_total",
    "profile_allocated_memory_mib_total",
    "app_workload_profile",
    "wp_current_count",
    "wp_min_count",
    "wp_max_count",
    "replicas_avg",
    "cpu_request",
    "memory_request_mib",
    "cpu_avg_cores",
    "memory_avg_mib",
    "cpu_utilisation_pct",
    "memory_utilisation_pct",
]


# Known ACA dedicated workload profile SKU capacities.
# Values are per instance.
WORKLOAD_PROFILE_SKU_CAPACITY = {
    "D4": (4.0, 16.0 * 1024),
    "D8": (8.0, 32.0 * 1024),
    "D16": (16.0, 64.0 * 1024),
    "D32": (32.0, 128.0 * 1024),
    "E4": (4.0, 32.0 * 1024),
    "E8": (8.0, 64.0 * 1024),
    "E16": (16.0, 128.0 * 1024),
    "E32": (32.0, 256.0 * 1024),
}

# Timeseries file: one row per app × timestamp
TIMESERIES_COLUMNS = [
    "resource_id",
    "timestamp",
    "cpu_series_value",
    "memory_series_value",
]


def extract_all_containers(resource):
    """Return list of container specs from app/job template. First is main, rest are sidecars."""
    template = getattr(resource, "template", None)
    containers = getattr(template, "containers", None) if template is not None else None
    if not containers:
        return []
    result = []
    for idx, container in enumerate(containers):
        name = getattr(container, "name", None)
        if not name:
            continue
        res = getattr(container, "resources", None)
        cpu = getattr(res, "cpu", None) if res is not None else None
        mem = getattr(res, "memory", None) if res is not None else None
        result.append(
            {
                "name": name,
                "is_sidecar": idx > 0,
                "cpu_request": float(cpu) if cpu is not None else None,
                "memory_request_mib": parse_memory_request_mib(mem),
            }
        )
    return result


def get_workload_sku_capacity(workload_profile_type):
    """Return (cpu, memory_mib) per instance for known dedicated profile SKUs."""
    if not workload_profile_type:
        return None, None
    key = str(workload_profile_type).strip().upper()
    return WORKLOAD_PROFILE_SKU_CAPACITY.get(key, (None, None))


# ── helpers ──────────────────────────────────────────────────────────

def parse_lookback(value: str) -> timedelta:
    """Accept an integer (minutes) or ISO-8601 duration like PT6H."""
    try:
        return timedelta(minutes=int(value))
    except ValueError:
        pass
    import re
    m = re.match(r"PT(?:(\d+)D)?(?:(\d+)H)?(?:(\d+)M)?", value, re.IGNORECASE)
    if m:
        days = int(m.group(1) or 0)
        hours = int(m.group(2) or 0)
        minutes = int(m.group(3) or 0)
        return timedelta(days=days, hours=hours, minutes=minutes)
    raise argparse.ArgumentTypeError(f"Cannot parse lookback '{value}'")


def read_customer_csv(json_path: str):
    """
    Read the customer JSON file.
    Returns a dict: { tenant_id: [ (subscription_id, subscription_name), ... ] }
    """
    if not os.path.exists(json_path):
        print(f"ERROR: Customer file not found: {json_path}")
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    tenant_map = defaultdict(list)
    for entry in data.get("azure", []):
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
    """Return an SDK credential without invoking Azure CLI login commands."""
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
    """List enabled subscriptions via Azure Resource Manager SDK."""
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


def avg_metric(
    monitor_client,
    resource_id,
    metric_name,
    timespan,
    interval="PT5M",
    log_errors=True,
):
    """Query Azure Monitor for the average of a metric over a timespan."""
    try:
        result = monitor_client.metrics.list(
            resource_uri=resource_id,
            metricnames=metric_name,
            aggregation="Average",
            timespan=timespan,
            interval=interval,
        )
        values = []
        for m in result.value:
            for ts in m.timeseries:
                for dp in ts.data:
                    if dp.average is not None:
                        values.append(dp.average)
        return round(sum(values) / len(values), 4) if values else None
    except Exception as exc:
        if log_errors:
            print(f"  ⚠  Metric query failed ({metric_name}): {exc}")
        return None


def get_metric_series(
    monitor_client,
    resource_id,
    metric_name,
    timespan,
    interval="PT5M",
    log_errors=False,
):
    """Return a list of (timestamp_str, value) tuples for a metric."""
    try:
        result = monitor_client.metrics.list(
            resource_uri=resource_id,
            metricnames=metric_name,
            aggregation="Average",
            timespan=timespan,
            interval=interval,
        )
        series = []
        for m in result.value:
            for ts in m.timeseries:
                for dp in ts.data:
                    ts_str = dp.time_stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if dp.time_stamp else ""
                    val = round(dp.average, 4) if dp.average is not None else None
                    series.append((ts_str, val))
        return series
    except Exception as exc:
        if log_errors:
            print(f"  ⚠  Metric series query failed ({metric_name}): {exc}")
        return []


def parse_memory_request_mib(mem_request):
    """Parse memory request values like 0.5Gi/512Mi to MiB."""
    if not mem_request:
        return None
    value = str(mem_request).strip()
    if not value:
        return None
    try:
        lower = value.lower()
        if lower.endswith("gi"):
            return float(lower[:-2]) * 1024
        if lower.endswith("mi"):
            return float(lower[:-2])
        return float(value)
    except ValueError:
        return None


def extract_environment_id(resource):
    """Return managed environment resource id for app/job objects."""
    direct = getattr(resource, "managed_environment_id", None)
    if direct:
        return str(direct)

    direct = getattr(resource, "environment_id", None)
    if direct:
        return str(direct)

    cfg = getattr(resource, "configuration", None)
    if cfg is not None:
        env_id = getattr(cfg, "environment_id", None)
        if env_id:
            return str(env_id)

    return ""


def extract_workload_profile(resource):
    """Return workload profile name for app/job objects when present."""
    wp = getattr(resource, "workload_profile_name", None)
    if wp:
        return str(wp)

    cfg = getattr(resource, "configuration", None)
    if cfg is not None:
        wp = getattr(cfg, "workload_profile_name", None)
        if wp:
            return str(wp)

    return ""


def extract_first_container_resources(resource):
    """Return (cpu_request, memory_request) from first container definition."""
    template = getattr(resource, "template", None)
    containers = getattr(template, "containers", None) if template is not None else None
    if not containers:
        return None, None

    first = containers[0]
    res = getattr(first, "resources", None)
    if res is None:
        return None, None

    return getattr(res, "cpu", None), getattr(res, "memory", None)


# ── process a single subscription ────────────────────────────────────

def process_subscription(credential, sub_id, sub_name, tenant_id, timespan):
    """Process one subscription and return (summary_rows, ts_rows)."""
    print(f"\n── Subscription: {sub_name or sub_id} ──")
    ca_client = ContainerAppsAPIClient(credential, sub_id)
    monitor_client = MonitorManagementClient(credential, sub_id)

    summary_rows: list[dict] = []
    ts_rows: list[dict] = []

    # List all managed environments in the subscription
    try:
        envs = list(ca_client.managed_environments.list_by_subscription())
    except Exception as exc:
        print(f"  ⚠  Could not list environments: {exc}")
        return summary_rows, ts_rows

    if not envs:
        print("  (no Container App environments found)")
        return summary_rows, ts_rows

    for env in envs:
        env_name = env.name
        env_rg = env.id.split("/resourceGroups/")[1].split("/")[0]
        env_id = env.id
        print(f"\n  Environment: {env_name}  (RG: {env_rg})")

        # Shared env fields
        env_base = {
            "tenant_id": tenant_id or "",
            "subscription_id": sub_id,
            "subscription_name": sub_name or "",
            "resource_group": env_rg,
            "environment_name": env_name,
            "location": env.location or "",
        }

        # ── Workload Profiles ────────────────────────────────
        # 1) Profile *definitions* come from the environment resource itself
        #    (env.workload_profiles → list of WorkloadProfile with name, type, min, max).
        # 2) Runtime *current_count* comes from list_workload_profile_states.
        wp_info = {}

        # Step 1: get definitions from the environment object
        env_profiles = getattr(env, "workload_profiles", None) or []
        for wp in env_profiles:
            wp_name = getattr(wp, "name", None) or ""
            wp_type = getattr(wp, "workload_profile_type", "") or ""
            min_c = getattr(wp, "minimum_count", None)
            max_c = getattr(wp, "maximum_count", None)
            if wp_name:
                wp_info[wp_name.lower()] = {
                    "workload_profile_name": wp_name,
                    "workload_profile_type": wp_type,
                    "wp_current_count": "",
                    "wp_min_count": min_c if min_c is not None else "",
                    "wp_max_count": max_c if max_c is not None else "",
                }

        # Step 2: enrich with current_count from runtime state
        try:
            states = list(
                ca_client.managed_environments.list_workload_profile_states(env_rg, env_name)
            )
            for st in states:
                props = getattr(st, "properties", None)
                st_name = getattr(props, "name", None) or st.name or ""
                current = getattr(props, "current_count", None)
                key = st_name.lower()
                if key in wp_info:
                    wp_info[key]["wp_current_count"] = current if current is not None else ""
                else:
                    # Profile returned by states but not in env definitions (shouldn't happen, but be safe)
                    st_type = getattr(props, "workload_profile_type", "unknown")
                    wp_info[key] = {
                        "workload_profile_name": st_name,
                        "workload_profile_type": st_type,
                        "wp_current_count": current if current is not None else "",
                        "wp_min_count": "",
                        "wp_max_count": "",
                    }
        except Exception as exc:
            print(f"    ⚠  Could not list workload profile states: {exc}")

        for wp_key, wp_data in wp_info.items():
            sku_cpu, sku_mem_mib = get_workload_sku_capacity(wp_data.get("workload_profile_type"))
            wp_data["workload_sku_cpu_per_instance"] = sku_cpu if sku_cpu is not None else ""
            wp_data["workload_sku_memory_mib_per_instance"] = sku_mem_mib if sku_mem_mib is not None else ""
            current_count = wp_data.get("wp_current_count")
            try:
                current_count_f = float(current_count) if current_count not in (None, "") else None
            except ValueError:
                current_count_f = None
            if current_count_f is not None and sku_cpu is not None:
                wp_data["profile_capacity_cpu_total"] = round(current_count_f * sku_cpu, 4)
            else:
                wp_data["profile_capacity_cpu_total"] = ""
            if current_count_f is not None and sku_mem_mib is not None:
                wp_data["profile_capacity_memory_mib_total"] = round(current_count_f * sku_mem_mib, 2)
            else:
                wp_data["profile_capacity_memory_mib_total"] = ""
            wp_data["profile_allocated_cpu_total"] = ""
            wp_data["profile_allocated_memory_mib_total"] = ""
            print(f"    Profile: {wp_data['workload_profile_name']} ({wp_data['workload_profile_type']}) — instances: {wp_data['wp_current_count']}")

        # ── Container Apps + Jobs & Metrics ──────────────────
        try:
            apps = list(ca_client.container_apps.list_by_resource_group(env_rg))
            apps = [a for a in apps if extract_environment_id(a).lower() == env_id.lower()]
        except Exception as exc:
            print(f"    ⚠  Could not list container apps: {exc}")
            apps = []

        try:
            jobs = list(ca_client.jobs.list_by_resource_group(env_rg))
            jobs = [j for j in jobs if extract_environment_id(j).lower() == env_id.lower()]
        except Exception as exc:
            print(f"    ⚠  Could not list container jobs: {exc}")
            jobs = []

        if not apps and not jobs:
            # Still emit one row per workload profile even if no workloads
            for wp_name, wp_data in wp_info.items():
                row = {**env_base, **wp_data}
                row.update({k: "" for k in SUMMARY_COLUMNS if k not in row})
                summary_rows.append(row)
            if not wp_info:
                # Empty environment — emit one row
                row = {k: "" for k in SUMMARY_COLUMNS}
                row.update(env_base)
                summary_rows.append(row)
            continue

        workloads = [("app", app) for app in apps] + [("job", job) for job in jobs]
        env_workload_rows = []
        profile_allocations = defaultdict(lambda: {"cpu": 0.0, "mem": 0.0})

        for workload_kind, workload in workloads:
            app_name = workload.name
            app_id = workload.id
            app_wp = extract_workload_profile(workload)
            memory_metric_name = "WorkingSetBytes" if workload_kind == "app" else "UsageBytes"

            # Get all containers (main + sidecars)
            container_specs = extract_all_containers(workload)
            if not container_specs:
                # Fallback when template has no explicit container list
                container_specs = [
                    {
                        "name": app_name,
                        "is_sidecar": False,
                        "cpu_request": None,
                        "memory_request_mib": None,
                    }
                ]

            print(f"    {workload_kind.title()}: {app_name}  (profile: {app_wp or 'n/a'})  [{len(container_specs)} container(s)]")

            # Query utilisation metrics
            cpu_avg = avg_metric(monitor_client, app_id, "UsageNanoCores", timespan)
            mem_avg = avg_metric(monitor_client, app_id, memory_metric_name, timespan)
            replica_count = (
                avg_metric(
                    monitor_client,
                    app_id,
                    "Replicas",
                    timespan,
                )
                if workload_kind == "app"
                else None
            )

            cpu_avg_cores = round(cpu_avg / 1e9, 4) if cpu_avg else None
            mem_avg_mib = round(mem_avg / (1024 * 1024), 2) if mem_avg else None

            # Get workload profile info for this app
            wp_data = wp_info.get((app_wp or "").lower(), {
                "workload_profile_name": app_wp,
                "workload_profile_type": "",
                "wp_current_count": "",
                "wp_min_count": "",
                "wp_max_count": "",
            })

            # Time series
            cpu_series = get_metric_series(monitor_client, app_id, "UsageNanoCores", timespan)
            mem_series = get_metric_series(monitor_client, app_id, memory_metric_name, timespan)

            # Convert series values
            cpu_series_conv = [
                (ts, round(v / 1e9, 4) if v is not None else None)
                for ts, v in cpu_series
            ]
            mem_series_conv = [
                (ts, round(v / (1024 * 1024), 2) if v is not None else None)
                for ts, v in mem_series
            ]

            # Merge CPU and memory series by timestamp
            ts_map = {}
            for ts, v in cpu_series_conv:
                ts_map.setdefault(ts, {"cpu": None, "mem": None})
                ts_map[ts]["cpu"] = v
            for ts, v in mem_series_conv:
                ts_map.setdefault(ts, {"cpu": None, "mem": None})
                ts_map[ts]["mem"] = v

            def fmt(val):
                return str(val) if val is not None else ""

            # Emit one row per container
            for container_spec in container_specs:
                container_name = container_spec["name"]
                is_sidecar = "1" if container_spec["is_sidecar"] else "0"
                cpu_req_f = container_spec["cpu_request"]
                mem_req_mib = container_spec["memory_request_mib"]
                cpu_util = round((cpu_avg_cores / cpu_req_f) * 100, 1) if cpu_avg_cores and cpu_req_f else None
                mem_util = round((mem_avg_mib / mem_req_mib) * 100, 1) if mem_avg_mib and mem_req_mib else None

                replicas_factor = replica_count if (workload_kind == "app" and replica_count is not None) else 1.0
                alloc_cpu = (cpu_req_f * replicas_factor) if cpu_req_f is not None else 0.0
                alloc_mem = (mem_req_mib * replicas_factor) if mem_req_mib is not None else 0.0
                if app_wp:
                    profile_allocations[app_wp.lower()]["cpu"] += alloc_cpu
                    profile_allocations[app_wp.lower()]["mem"] += alloc_mem

                # Summary row — one per container, no timestamps
                summary_row = {
                    **env_base,
                    **wp_data,
                    "app_name": app_name,
                    "workload_kind": workload_kind,
                    "container_name": container_name,
                    "is_sidecar": is_sidecar,
                    "resource_id": app_id,
                    "app_workload_profile": app_wp or "",
                    "replicas_avg": fmt(round(replica_count, 1) if replica_count else None),
                    "cpu_request": fmt(cpu_req_f),
                    "memory_request_mib": fmt(mem_req_mib),
                    "cpu_avg_cores": fmt(cpu_avg_cores),
                    "memory_avg_mib": fmt(mem_avg_mib),
                    "cpu_utilisation_pct": fmt(cpu_util),
                    "memory_utilisation_pct": fmt(mem_util),
                }
                env_workload_rows.append(summary_row)

            # Timeseries rows — one per app×timestamp (inside the workload loop)
            for ts_key in sorted(ts_map.keys()):
                vals = ts_map[ts_key]
                ts_rows.append({
                    "resource_id": app_id,
                    "timestamp": ts_key,
                    "cpu_series_value": fmt(vals["cpu"]),
                    "memory_series_value": fmt(vals["mem"]),
                })

        # Add per-profile allocated totals to every matching workload row.
        for row in env_workload_rows:
            p = row.get("app_workload_profile", "").lower()
            if p and p in profile_allocations:
                row["profile_allocated_cpu_total"] = str(round(profile_allocations[p]["cpu"], 4))
                row["profile_allocated_memory_mib_total"] = str(round(profile_allocations[p]["mem"], 2))

        # Ensure profile-only rows also carry allocation and capacity info.
        for wp_key, wp_data in wp_info.items():
            profile_name = wp_data.get("workload_profile_name", "").lower()
            if profile_name and profile_name in profile_allocations:
                wp_data["profile_allocated_cpu_total"] = round(profile_allocations[profile_name]["cpu"], 4)
                wp_data["profile_allocated_memory_mib_total"] = round(profile_allocations[profile_name]["mem"], 2)

        summary_rows.extend(env_workload_rows)

    return summary_rows, ts_rows


def parse_date(value: str) -> datetime:
    """Parse a date string (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS) into a UTC datetime."""
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Cannot parse date '{value}'. Use YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS."
    )


# ── main logic ───────────────────────────────────────────────────────

def export(args):
    now = datetime.now(timezone.utc)

    # Determine timespan: --from/--to takes precedence over --lookback
    if args.date_from:
        start = parse_date(args.date_from)
        end = parse_date(args.date_to) if args.date_to else now
        if start >= end:
            print(f"ERROR: --from ({args.date_from}) must be before --to ({args.date_to or 'now'})")
            sys.exit(1)
        print(f"📅  Date range: {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        lookback = parse_lookback(args.lookback or "60")
        start = now - lookback
        end = now

    # Use Z suffix to avoid +00:00 encoding issues with Azure Monitor
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    timespan = f"{start.strftime(fmt)}/{end.strftime(fmt)}"

    all_summary: list[dict] = []
    all_ts: list[dict] = []

    if args.input:
        # ── CSV batch mode ─────────────────────────────────────
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
                s, t = process_subscription(credential, sub_id, sub_name, tenant_id, timespan)
                all_summary.extend(s)
                all_ts.extend(t)

    elif args.subscription:
        credential = get_credential()
        s, t = process_subscription(credential, args.subscription, None, "", timespan)
        all_summary.extend(s)
        all_ts.extend(t)

    else:
        credential = get_credential(None, args.sp_client_id, args.sp_client_secret, args.sp_certificate)
        subs = list_enabled_subscriptions(credential)
        print(f"Found {len(subs)} enabled subscription(s) via ARM SDK")
        for sub_id, sub_name, tenant_id in subs:
            s, t = process_subscription(credential, sub_id, sub_name, tenant_id, timespan)
            all_summary.extend(s)
            all_ts.extend(t)

    # ── Write CSV output ─────────────────────────────────────
    os.makedirs(args.output_dir, exist_ok=True)
    date_range_str = f"{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}"
    if args.input:
        prefix = os.path.splitext(os.path.basename(args.input))[0].upper()
        summary_filename = f"{prefix}_container_apps_summary_{date_range_str}.csv"
        ts_filename      = f"{prefix}_container_apps_timeseries_{date_range_str}.csv"
    else:
        summary_filename = f"container_apps_summary_{date_range_str}.csv"
        ts_filename      = f"container_apps_timeseries_{date_range_str}.csv"

    summary_path = os.path.join(args.output_dir, summary_filename)
    ts_path      = os.path.join(args.output_dir, ts_filename)
    summary_json_path = os.path.join(args.output_dir, summary_filename.replace(".csv", ".json"))
    ts_json_path = os.path.join(args.output_dir, ts_filename.replace(".csv", ".json"))

    if args.output_format in ("csv", "both"):
        with open(summary_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(all_summary)

        with open(ts_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TIMESERIES_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(all_ts)

    if args.output_format in ("json", "both"):
        with open(summary_json_path, "w", encoding="utf-8") as f:
            json.dump(all_summary, f, indent=2)

        with open(ts_json_path, "w", encoding="utf-8") as f:
            json.dump(all_ts, f, indent=2)

    # Count unique workloads and environments
    unique_workloads = len(set(
        (r.get("workload_kind", "app"), r.get("app_name", ""))
        for r in all_summary if r.get("app_name")
    ))
    unique_apps = len(set(
        r.get("app_name", "")
        for r in all_summary
        if r.get("app_name") and (r.get("workload_kind") in ("", "app"))
    ))
    unique_jobs = len(set(
        r.get("app_name", "")
        for r in all_summary
        if r.get("app_name") and r.get("workload_kind") == "job"
    ))
    unique_envs = len(set(r.get("environment_name", "") for r in all_summary if r.get("environment_name")))
    unique_profiles = len(set(
        (r.get("environment_name", ""), r.get("workload_profile_name", ""))
        for r in all_summary if r.get("workload_profile_name")
    ))

    print(f"\n✅  Exported {unique_envs} environment(s), "
          f"{unique_profiles} workload profile(s), {unique_workloads} workload(s) "
          f"({unique_apps} app(s), {unique_jobs} job(s)), "
          f"{len(all_summary)} summary row(s), {len(all_ts)} timeseries row(s)")
    if args.output_format in ("csv", "both"):
        print(f"    → {summary_path}")
        print(f"    → {ts_path}")
    if args.output_format in ("json", "both"):
        print(f"    → {summary_json_path}")
        print(f"    → {ts_json_path}")


# ── CLI ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export Azure Container Apps utilisation data (get_containerApps).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
    %(prog)s -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.csv --lookback PT6H --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.csv --from 2026-01-01 --to 2026-03-26 --output-dir ./reports/CUST
  %(prog)s -s <subscription-id> --from 2026-03-01 --output-dir ./reports

output files:
    containerApps_<CSVNAME>.csv    (CSV input mode)
  containerApps_export.csv       (single subscription / all subscriptions mode)
        """,
    )

    parser.add_argument("-i", "--input", default=None,
                        help="Path to semicolon-delimited customer CSV (all rows processed)")
    parser.add_argument("-s", "--subscription",
                        help="Single subscription ID")
    parser.add_argument("--skip-login", action="store_true",
                        help="Deprecated no-op; kept for backward compatibility")
    parser.add_argument("--sp-client-id", default=None, metavar="APP_ID",
                        help="App Registration client ID for non-interactive service principal login.")
    parser.add_argument("--sp-client-secret",
                        default=os.environ.get("AZURE_SP_CLIENT_SECRET"),
                        metavar="SECRET",
                        help="Client secret for service principal login. "
                             "Falls back to AZURE_SP_CLIENT_SECRET env var.")
    parser.add_argument("--sp-certificate", default=None, metavar="CERT_PATH",
                        help="Path to PEM certificate for service principal auth "
                             "(alternative to --sp-client-secret).")

    parser.add_argument("--output-dir", default=".",
                        help="Output directory for generated files (default: current dir). Created if it doesn't exist.")
    parser.add_argument("--output-format", choices=("csv", "json", "both"), default="both",
                        help="File output format: csv, json, or both (default: both)")

    parser.add_argument("--lookback", "-l", default=None,
                        help="Metrics lookback window: minutes (int) or ISO duration "
                             "like PT6H, PT30M (default: 60 minutes if --from/--to not set)")

    parser.add_argument("--from", dest="date_from", default=None,
                        help="Start date for metrics (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). "
                             "Use with --to for an absolute date range.")
    parser.add_argument("--to", dest="date_to", default=None,
                        help="End date for metrics (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). "
                             "Defaults to now if --from is set but --to is omitted.")

    args = parser.parse_args()

    if args.date_from and args.lookback:
        parser.error("--from/--to and --lookback are mutually exclusive. Use one or the other.")

    if args.date_to and not args.date_from:
        parser.error("--to requires --from")

    if not args.input and not args.subscription:
        print("ℹ  No -i or -s given — will scan all subscriptions in the default credential scope.")

    export(args)


if __name__ == "__main__":
    main()
