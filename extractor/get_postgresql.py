#!/usr/bin/env python3
"""
Azure Database for PostgreSQL Flexible Server - Utilization Exporter
===================================================================
Exports PostgreSQL Flexible Server inventory and Azure Monitor metrics into
two files that fit the existing dashboarding/reporting contract used in this
repo:

  *_postgresql_summary_*.csv
      One row per server with inventory, configuration, utilization summaries,
      and a simple advisory classification.

  *_postgresql_timeseries_*.csv
      One row per (server, timestamp) with utilization points suitable for
      sparklines and trend views.

Output: semicolon-delimited, BOM-prefixed CSV (Excel-compatible).

Prerequisites
-------------
    pip install azure-identity azure-mgmt-monitor requests

Authentication
--------------
    Same contract as the other extractors in this repo: reads the customer
    JSON (-i), groups subscriptions by tenant, reuses the existing az session
    (unless --skip-login is omitted and the wrapper logs in first), and then
    iterates the matching subscriptions.

Usage
-----
    python get_postgresql.py -i ../customers/CUST.json --skip-login --output-dir ./reports/CUST
    python get_postgresql.py -i ../customers/CUST.json --lookback PT24H --output-dir ./reports/CUST
    python get_postgresql.py -s <subscription-id> --from 2026-01-01 --to 2026-03-31 --output-dir ./reports
"""

import argparse
import csv
import json
import os
import requests
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


_REQUIRED = {
    "azure.identity": "azure-identity",
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
        print(f"    -  {pkg}")
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


SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "server_name",
    "server_id",
    "location",
    "version",
    "state",
    "create_mode",
    "replication_role",
    "administrator_login",
    "fully_qualified_domain_name",
    "sku_name",
    "sku_tier",
    "compute_tier",
    "vcores",
    "storage_size_gib",
    "storage_tier",
    "storage_auto_grow",
    "storage_used_avg_mib",
    "storage_used_peak_mib",
    "storage_pct_avg",
    "storage_pct_peak",
    "backup_retention_days",
    "geo_redundant_backup",
    "backup_storage_used_avg_mib",
    "high_availability_mode",
    "standby_availability_zone",
    "availability_zone",
    "public_network_access",
    "delegated_subnet_id",
    "private_dns_zone_id",
    "active_directory_auth_enabled",
    "password_auth_enabled",
    "iops_avg",
    "iops_peak",
    "cpu_avg_pct",
    "cpu_p95_pct",
    "cpu_peak_pct",
    "memory_avg_pct",
    "memory_p95_pct",
    "memory_peak_pct",
    "active_connections_avg",
    "active_connections_peak",
    "max_connections_peak",
    "connections_failed_total",
    "network_ingress_total_mib",
    "network_egress_total_mib",
    "replication_lag_peak_sec",
    "observed_sample_count",
    "advisory_category",
    "advisory_reason",
    "tags",
]

TIMESERIES_COLUMNS = [
    "server_id",
    "timestamp",
    "cpu_percent",
    "memory_percent",
    "storage_percent",
    "storage_used",
    "active_connections",
    "max_connections",
    "iops",
    "connections_failed",
    "network_bytes_ingress",
    "network_bytes_egress",
    "backup_storage_used",
    "replication_lag_seconds",
]

POSTGRES_METRICS = [
    {"azure_name": "cpu_percent", "column": "cpu_percent", "aggregation": "Average"},
    {"azure_name": "memory_percent", "column": "memory_percent", "aggregation": "Average"},
    {"azure_name": "storage_percent", "column": "storage_percent", "aggregation": "Average"},
    {"azure_name": "storage_used", "column": "storage_used", "aggregation": "Average"},
    {"azure_name": "active_connections", "column": "active_connections", "aggregation": "Average"},
    {"azure_name": "max_connections", "column": "max_connections", "aggregation": "Maximum"},
    {"azure_name": "iops", "column": "iops", "aggregation": "Average"},
    {"azure_name": "connections_failed", "column": "connections_failed", "aggregation": "Total"},
    {"azure_name": "network_bytes_ingress", "column": "network_bytes_ingress", "aggregation": "Total"},
    {"azure_name": "network_bytes_egress", "column": "network_bytes_egress", "aggregation": "Total"},
    {"azure_name": "backup_storage_used", "column": "backup_storage_used", "aggregation": "Average"},
    {
        "azure_name": "physical_replication_delay_in_seconds",
        "column": "replication_lag_seconds",
        "aggregation": "Maximum",
    },
]

POSTGRES_API_VERSION = "2025-08-01"


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


def parse_resource_group(resource_id: str) -> str:
    if not resource_id or "/resourceGroups/" not in resource_id:
        return ""
    return resource_id.split("/resourceGroups/")[1].split("/")[0]


def tags_str(tags: dict | None) -> str:
    if not tags:
        return ""
    return ", ".join(f"{k}={v}" for k, v in sorted(tags.items()))


def dig(data, *path, default=None):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


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


def pick_monitor_interval(start: datetime, end: datetime) -> str:
    span = end - start
    if span <= timedelta(days=1):
        return "PT5M"
    if span <= timedelta(days=7):
        return "PT15M"
    if span <= timedelta(days=31):
        return "PT1H"
    return "PT6H"


def bytes_to_mib(value):
    if value is None:
        return None
    return value / (1024.0 * 1024.0)


def get_metric_value(point, aggregation: str):
    agg = (aggregation or "Average").lower()
    if agg == "average":
        candidates = [getattr(point, "average", None), getattr(point, "maximum", None), getattr(point, "total", None)]
    elif agg == "total":
        candidates = [getattr(point, "total", None), getattr(point, "average", None), getattr(point, "maximum", None)]
    elif agg == "maximum":
        candidates = [getattr(point, "maximum", None), getattr(point, "average", None), getattr(point, "total", None)]
    else:
        candidates = [
            getattr(point, "average", None),
            getattr(point, "maximum", None),
            getattr(point, "total", None),
            getattr(point, "minimum", None),
        ]
    for candidate in candidates:
        if candidate is not None:
            return float(candidate)
    return None


def get_metric_series(
    monitor_client,
    resource_id: str,
    metric_name: str,
    aggregation: str,
    timespan: str,
    interval: str,
    log_errors: bool = False,
):
    try:
        result = monitor_client.metrics.list(
            resource_uri=resource_id,
            metricnames=metric_name,
            aggregation=aggregation,
            timespan=timespan,
            interval=interval,
        )
        series = []
        for metric in result.value:
            for ts in metric.timeseries or []:
                for point in ts.data or []:
                    value = get_metric_value(point, aggregation)
                    timestamp = getattr(point, "time_stamp", None)
                    if value is None or timestamp is None:
                        continue
                    series.append((timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"), value))
        return series
    except Exception as exc:
        if log_errors:
            print(f"    WARN metric '{metric_name}' failed for {resource_id}: {exc}")
        return []


def summarize_series(values: list[float], total_metric: bool = False) -> dict:
    clean = [v for v in values if v is not None]
    if not clean:
        return {"samples": 0, "avg": None, "p95": None, "peak": None, "total": None}
    return {
        "samples": len(clean),
        "avg": sum(clean) / len(clean),
        "p95": percentile(clean, 95),
        "peak": max(clean),
        "total": sum(clean) if total_metric else None,
    }


def classify_server(summary: dict, sku_tier: str) -> tuple[str, str]:
    cpu_p95 = summary["cpu_percent"]["p95"]
    memory_p95 = summary["memory_percent"]["p95"]
    storage_peak = summary["storage_percent"]["peak"]
    conn_peak = summary["active_connections"]["peak"]
    max_conn = summary["max_connections"]["peak"]
    ingress_total = summary["network_bytes_ingress"]["total"] or 0.0
    egress_total = summary["network_bytes_egress"]["total"] or 0.0
    replication_peak = summary["replication_lag_seconds"]["peak"]

    if storage_peak is not None and storage_peak >= 85:
        return "storage-pressure", "Storage usage peaked above 85%; review storage growth or tier sizing."

    if (
        (cpu_p95 is not None and cpu_p95 >= 80)
        or (memory_p95 is not None and memory_p95 >= 85)
        or (max_conn and conn_peak is not None and max_conn > 0 and (conn_peak / max_conn) >= 0.8)
    ):
        return "scale-up-review", "Sustained CPU, memory, or connection pressure suggests a sizing review."

    if replication_peak is not None and replication_peak >= 300:
        return "replication-review", "Observed read replica lag above 5 minutes; review replica health or workload split."

    low_traffic = (ingress_total + egress_total) < (512 * 1024 * 1024)
    if (
        (cpu_p95 is None or cpu_p95 < 10)
        and (memory_p95 is None or memory_p95 < 20)
        and (conn_peak is None or conn_peak <= 5)
        and low_traffic
    ):
        return "low-utilization", "Low sustained utilization and low traffic suggest a rightsizing review."

    if "burstable" in (sku_tier or "").lower() and cpu_p95 is not None and cpu_p95 >= 60:
        return "burstable-review", "Burstable SKU shows steady utilization; review whether a general-purpose tier is a better fit."

    return "healthy", "No clear pressure or oversizing signal from the observed metric window."


def list_postgresql_servers(credential, subscription_id: str) -> list[dict]:
    token = get_token(credential)
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.DBforPostgreSQL/flexibleServers"
        f"?api-version={POSTGRES_API_VERSION}"
    )
    items: list[dict] = []
    while url:
        response = requests.get(url, headers=headers, timeout=120)
        response.raise_for_status()
        payload = response.json()
        items.extend(payload.get("value", []))
        url = payload.get("nextLink")
    return items


def process_subscription(credential, sub_id: str, sub_name: str | None, tenant_id: str, timespan: str, interval: str):
    print(f"\n-- Subscription: {sub_name or sub_id} --")
    monitor_client = MonitorManagementClient(credential, sub_id)

    try:
        servers = list_postgresql_servers(credential, sub_id)
    except Exception as exc:
        print(f"  WARN Could not list PostgreSQL flexible servers: {exc}")
        return [], []

    if not servers:
        print("  (no PostgreSQL flexible servers found)")
        return [], []

    summary_rows = []
    ts_rows = []

    for server in servers:
        server_id = str(server.get("id") or "")
        server_name = str(server.get("name") or "")
        resource_group = parse_resource_group(server_id)
        print(f"  Server: {server_name}  (RG: {resource_group})")

        props = server.get("properties") or {}
        sku = server.get("sku") or {}
        storage = props.get("storage") or {}
        backup = props.get("backup") or {}
        network = props.get("network") or {}
        ha = props.get("highAvailability") or {}
        auth = props.get("authConfig") or {}

        combined_rows: dict[str, dict] = {}
        metric_summaries: dict[str, dict] = {}

        for metric in POSTGRES_METRICS:
            series = get_metric_series(
                monitor_client,
                server_id,
                metric["azure_name"],
                metric["aggregation"],
                timespan,
                interval,
            )
            values = [value for _timestamp, value in series]
            metric_summaries[metric["column"]] = summarize_series(
                values,
                total_metric=metric["aggregation"].lower() == "total",
            )
            for timestamp, value in series:
                row = combined_rows.setdefault(
                    timestamp,
                    {column: "" for column in TIMESERIES_COLUMNS},
                )
                row["server_id"] = server_id
                row["timestamp"] = timestamp
                row[metric["column"]] = round(value, 4)

        ordered_ts = [combined_rows[key] for key in sorted(combined_rows.keys())]
        ts_rows.extend(ordered_ts)

        advisory_category, advisory_reason = classify_server(metric_summaries, str(sku.get("tier") or ""))

        summary_rows.append(
            {
                "tenant_id": tenant_id or "",
                "subscription_id": sub_id,
                "subscription_name": sub_name or "",
                "resource_group": resource_group,
                "server_name": server_name,
                "server_id": server_id,
                "location": server.get("location") or "",
                "version": props.get("version") or "",
                "state": props.get("state") or "",
                "create_mode": props.get("createMode") or "",
                "replication_role": props.get("replicationRole") or "",
                "administrator_login": props.get("administratorLogin") or "",
                "fully_qualified_domain_name": props.get("fullyQualifiedDomainName") or "",
                "sku_name": sku.get("name") or "",
                "sku_tier": sku.get("tier") or "",
                "compute_tier": props.get("tier") or dig(props, "storage", "tier") or "",
                "vcores": sku.get("capacity") or "",
                "storage_size_gib": storage.get("storageSizeGB") or "",
                "storage_tier": storage.get("tier") or "",
                "storage_auto_grow": storage.get("autoGrow") or "",
                "storage_used_avg_mib": round(bytes_to_mib(metric_summaries["storage_used"]["avg"]), 2) if metric_summaries["storage_used"]["avg"] is not None else "",
                "storage_used_peak_mib": round(bytes_to_mib(metric_summaries["storage_used"]["peak"]), 2) if metric_summaries["storage_used"]["peak"] is not None else "",
                "storage_pct_avg": round(metric_summaries["storage_percent"]["avg"], 2) if metric_summaries["storage_percent"]["avg"] is not None else "",
                "storage_pct_peak": round(metric_summaries["storage_percent"]["peak"], 2) if metric_summaries["storage_percent"]["peak"] is not None else "",
                "backup_retention_days": backup.get("backupRetentionDays") or "",
                "geo_redundant_backup": backup.get("geoRedundantBackup") or "",
                "backup_storage_used_avg_mib": round(bytes_to_mib(metric_summaries["backup_storage_used"]["avg"]), 2) if metric_summaries["backup_storage_used"]["avg"] is not None else "",
                "high_availability_mode": ha.get("mode") or "",
                "standby_availability_zone": ha.get("standbyAvailabilityZone") or "",
                "availability_zone": props.get("availabilityZone") or "",
                "public_network_access": network.get("publicNetworkAccess") or "",
                "delegated_subnet_id": network.get("delegatedSubnetResourceId") or "",
                "private_dns_zone_id": network.get("privateDnsZoneArmResourceId") or "",
                "active_directory_auth_enabled": auth.get("activeDirectoryAuth") or "",
                "password_auth_enabled": auth.get("passwordAuth") or "",
                "iops_avg": round(metric_summaries["iops"]["avg"], 2) if metric_summaries["iops"]["avg"] is not None else "",
                "iops_peak": round(metric_summaries["iops"]["peak"], 2) if metric_summaries["iops"]["peak"] is not None else "",
                "cpu_avg_pct": round(metric_summaries["cpu_percent"]["avg"], 2) if metric_summaries["cpu_percent"]["avg"] is not None else "",
                "cpu_p95_pct": round(metric_summaries["cpu_percent"]["p95"], 2) if metric_summaries["cpu_percent"]["p95"] is not None else "",
                "cpu_peak_pct": round(metric_summaries["cpu_percent"]["peak"], 2) if metric_summaries["cpu_percent"]["peak"] is not None else "",
                "memory_avg_pct": round(metric_summaries["memory_percent"]["avg"], 2) if metric_summaries["memory_percent"]["avg"] is not None else "",
                "memory_p95_pct": round(metric_summaries["memory_percent"]["p95"], 2) if metric_summaries["memory_percent"]["p95"] is not None else "",
                "memory_peak_pct": round(metric_summaries["memory_percent"]["peak"], 2) if metric_summaries["memory_percent"]["peak"] is not None else "",
                "active_connections_avg": round(metric_summaries["active_connections"]["avg"], 2) if metric_summaries["active_connections"]["avg"] is not None else "",
                "active_connections_peak": round(metric_summaries["active_connections"]["peak"], 2) if metric_summaries["active_connections"]["peak"] is not None else "",
                "max_connections_peak": round(metric_summaries["max_connections"]["peak"], 2) if metric_summaries["max_connections"]["peak"] is not None else "",
                "connections_failed_total": round(metric_summaries["connections_failed"]["total"], 2) if metric_summaries["connections_failed"]["total"] is not None else "",
                "network_ingress_total_mib": round(bytes_to_mib(metric_summaries["network_bytes_ingress"]["total"]), 2) if metric_summaries["network_bytes_ingress"]["total"] is not None else "",
                "network_egress_total_mib": round(bytes_to_mib(metric_summaries["network_bytes_egress"]["total"]), 2) if metric_summaries["network_bytes_egress"]["total"] is not None else "",
                "replication_lag_peak_sec": round(metric_summaries["replication_lag_seconds"]["peak"], 2) if metric_summaries["replication_lag_seconds"]["peak"] is not None else "",
                "observed_sample_count": max((item["samples"] for item in metric_summaries.values()), default=0),
                "advisory_category": advisory_category,
                "advisory_reason": advisory_reason,
                "tags": tags_str(server.get("tags") or {}),
            }
        )

    return summary_rows, ts_rows


def export(args):
    now = datetime.now(timezone.utc)

    if args.date_from:
        start = parse_date(args.date_from)
        end = parse_date(args.date_to) if args.date_to else now
        if start >= end:
            raise SystemExit("ERROR: --from must be earlier than --to")
        print(f"Date range: {start.strftime('%Y-%m-%d %H:%M')} -> {end.strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        lookback = parse_lookback(args.lookback or "1440")
        start = now - lookback
        end = now

    fmt = "%Y-%m-%dT%H:%M:%SZ"
    timespan = f"{start.strftime(fmt)}/{end.strftime(fmt)}"
    interval = pick_monitor_interval(start, end)

    all_summary: list[dict] = []
    all_ts: list[dict] = []

    if args.input:
        tenant_map = read_customer_csv(args.input)
        for tenant_id, subs in tenant_map.items():
            credential = get_credential(
                tenant_id,
                args.sp_client_id,
                args.sp_client_secret,
                args.sp_certificate,
            )
            for sub_id, sub_name in subs:
                summary_rows, ts_rows = process_subscription(
                    credential,
                    sub_id,
                    sub_name,
                    tenant_id,
                    timespan,
                    interval,
                )
                all_summary.extend(summary_rows)
                all_ts.extend(ts_rows)

    elif args.subscription:
        credential = get_credential(None, args.sp_client_id, args.sp_client_secret, args.sp_certificate)
        summary_rows, ts_rows = process_subscription(credential, args.subscription, None, "", timespan, interval)
        all_summary.extend(summary_rows)
        all_ts.extend(ts_rows)

    else:
        credential = get_credential(None, args.sp_client_id, args.sp_client_secret, args.sp_certificate)
        subs = list_enabled_subscriptions(credential)
        print(f"Found {len(subs)} enabled subscription(s) via ARM SDK")
        for sub_id, sub_name, tenant_id in subs:
            summary_rows, ts_rows = process_subscription(
                credential,
                sub_id,
                sub_name,
                tenant_id,
                timespan,
                interval,
            )
            all_summary.extend(summary_rows)
            all_ts.extend(ts_rows)

    os.makedirs(args.output_dir, exist_ok=True)
    date_range_str = f"{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}"
    if args.input:
        prefix = os.path.splitext(os.path.basename(args.input))[0].upper()
        summary_filename = f"{prefix}_postgresql_summary_{date_range_str}.csv"
        ts_filename = f"{prefix}_postgresql_timeseries_{date_range_str}.csv"
    else:
        summary_filename = f"postgresql_summary_{date_range_str}.csv"
        ts_filename = f"postgresql_timeseries_{date_range_str}.csv"

    summary_path = os.path.join(args.output_dir, summary_filename)
    ts_path = os.path.join(args.output_dir, ts_filename)
    summary_json_path = os.path.join(args.output_dir, summary_filename.replace(".csv", ".json"))
    ts_json_path = os.path.join(args.output_dir, ts_filename.replace(".csv", ".json"))

    if args.output_format in ("csv", "both"):
        with open(summary_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS, delimiter=";", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_summary)

        with open(ts_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TIMESERIES_COLUMNS, delimiter=";", extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_ts)

    if args.output_format in ("json", "both"):
        with open(summary_json_path, "w", encoding="utf-8") as handle:
            json.dump(all_summary, handle, indent=2, ensure_ascii=False)

        with open(ts_json_path, "w", encoding="utf-8") as handle:
            json.dump(all_ts, handle, indent=2, ensure_ascii=False)

    print(
        f"\nExported {len(all_summary)} summary row(s) and {len(all_ts)} timeseries row(s)"
    )
    if args.output_format in ("csv", "both"):
        print(f"  -> {summary_path}")
        print(f"  -> {ts_path}")
    if args.output_format in ("json", "both"):
        print(f"  -> {summary_json_path}")
        print(f"  -> {ts_json_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Export Azure Database for PostgreSQL Flexible Server utilization data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
    %(prog)s -i ../customers/CUST.json --skip-login --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.json --lookback PT24H --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.json --from 2026-01-01 --to 2026-03-26 --output-dir ./reports/CUST
    %(prog)s -s <subscription-id> --from 2026-03-01 --output-dir ./reports
        """,
    )

    parser.add_argument("-i", "--input", default=None, help="Path to customer JSON file")
    parser.add_argument("-s", "--subscription", help="Single subscription ID")
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
        help="Client secret for service principal login. Falls back to AZURE_SP_CLIENT_SECRET env var.",
    )
    parser.add_argument(
        "--sp-certificate",
        default=None,
        metavar="CERT_PATH",
        help="Path to PEM certificate for service principal auth (alternative to --sp-client-secret).",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Output directory for generated files (default: current dir).",
    )
    parser.add_argument(
        "--output-format",
        choices=("csv", "json", "both"),
        default="both",
        help="File output format: csv, json, or both (default: both)",
    )
    parser.add_argument(
        "--lookback",
        "-l",
        default=None,
        help="Metrics lookback window: minutes (int) or ISO duration like PT24H.",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Start date for metrics (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="End date for metrics (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). Defaults to now when omitted.",
    )

    args = parser.parse_args()

    if args.date_from and args.lookback:
        parser.error("--from/--to and --lookback are mutually exclusive. Use one or the other.")

    if args.date_to and not args.date_from:
        parser.error("--to requires --from")

    if not args.input and not args.subscription:
        print("INFO: No -i or -s given; will scan all subscriptions in the default credential scope.")

    export(args)


if __name__ == "__main__":
    main()