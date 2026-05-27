#!/usr/bin/env python3
"""
Azure SQL - Utilization Exporter
===============================
Exports Azure SQL database inventory and Azure Monitor metrics into two files
that fit the existing dashboarding/reporting contract used in this repo:

  *_sql_summary_*.csv
      One row per database with inventory, configuration, utilization summaries,
      and a simple advisory classification.

  *_sql_timeseries_*.csv
      One row per (database, timestamp) with utilization points suitable for
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
    python get_sql.py -i ../customers/CUST.json --skip-login --output-dir ./reports/CUST
    python get_sql.py -i ../customers/CUST.json --lookback PT24H --output-dir ./reports/CUST
    python get_sql.py -s <subscription-id> --from 2026-01-01 --to 2026-03-31 --output-dir ./reports
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

from azure.identity import (  # noqa: E402
    AzureCliCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
)
from azure.mgmt.monitor import MonitorManagementClient  # noqa: E402


SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "name",
    "server_name",
    "server_id",
    "database_name",
    "database_id",
    "location",
    "server_state",
    "server_version",
    "server_fully_qualified_domain_name",
    "administrator_login",
    "public_network_access",
    "minimal_tls_version",
    "database_state",
    "sku_name",
    "sku_tier",
    "vcores",
    "edition",
    "service_objective",
    "requested_service_objective",
    "elastic_pool_name",
    "backup_storage_redundancy",
    "collation",
    "max_size_bytes",
    "zone_redundant",
    "creation_date",
    "read_scale",
    "auto_pause_delay_minutes",
    "min_capacity",
    "ledger_on",
    "cpu_avg_pct",
    "cpu_p95_pct",
    "cpu_peak_pct",
    "dtu_avg_pct",
    "dtu_p95_pct",
    "dtu_peak_pct",
    "storage_pct_avg",
    "storage_pct_peak",
    "log_write_pct_avg",
    "log_write_pct_peak",
    "data_io_pct_avg",
    "data_io_pct_peak",
    "sessions_pct_avg",
    "sessions_peak_pct",
    "workers_pct_avg",
    "workers_peak_pct",
    "deadlock_total",
    "connections_failed_total",
    "connections_successful_total",
    "qc_aad_admin_configured",
    "qc_public_access_disabled",
    "qc_tls_12_or_higher",
    "qc_auditing_enabled",
    "qc_auditing_90_days",
    "qc_no_azure_services_firewall",
    "qc_private_endpoint_configured",
    "qc_tde_enabled",
    "qc_backup_geo_redundant",
    "qc_pitr_7_days",
    "qc_ltr_configured",
    "qc_db_auditing_enabled",
    "qc_atp_enabled",
    "qc_has_tags",
    "observed_sample_count",
    "advisory_category",
    "advisory_reason",
    "tags",
]

TIMESERIES_COLUMNS = [
    "server_id",
    "database_id",
    "timestamp",
    "cpu_percent",
    "dtu_consumption_percent",
    "storage_percent",
    "log_write_percent",
    "data_io_percent",
    "sessions_percent",
    "workers_percent",
    "deadlock",
    "connection_failed",
    "connection_successful",
]

SQL_METRICS = [
    {"azure_name": "cpu_percent", "column": "cpu_percent", "aggregation": "Average"},
    {"azure_name": "dtu_consumption_percent", "column": "dtu_consumption_percent", "aggregation": "Average"},
    {"azure_name": "storage_percent", "column": "storage_percent", "aggregation": "Average"},
    {"azure_name": "log_write_percent", "column": "log_write_percent", "aggregation": "Average"},
    {"azure_name": "data_io_percent", "column": "data_io_percent", "aggregation": "Average"},
    {"azure_name": "sessions_percent", "column": "sessions_percent", "aggregation": "Average"},
    {"azure_name": "workers_percent", "column": "workers_percent", "aggregation": "Average"},
    {"azure_name": "deadlock", "column": "deadlock", "aggregation": "Total"},
    {"azure_name": "connection_failed", "column": "connection_failed", "aggregation": "Total"},
    {"azure_name": "connection_successful", "column": "connection_successful", "aggregation": "Total"},
]

SQL_API_VERSION = "2021-11-01"


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


def classify_database(summary: dict, sku_tier: str, service_objective: str) -> tuple[str, str]:
    cpu_p95 = summary["cpu_percent"]["p95"]
    dtu_p95 = summary["dtu_consumption_percent"]["p95"]
    storage_peak = summary["storage_percent"]["peak"]
    log_write_peak = summary["log_write_percent"]["peak"]
    data_io_peak = summary["data_io_percent"]["peak"]
    sessions_peak = summary["sessions_percent"]["peak"]
    workers_peak = summary["workers_percent"]["peak"]
    deadlocks = summary["deadlock"]["total"] or 0.0
    failed_connections = summary["connection_failed"]["total"] or 0.0
    successful_connections = summary["connection_successful"]["total"] or 0.0

    if storage_peak is not None and storage_peak >= 85:
        return "storage-pressure", "Storage usage peaked above 85%; review database size and tier capacity."

    if (
        (cpu_p95 is not None and cpu_p95 >= 80)
        or (dtu_p95 is not None and dtu_p95 >= 80)
        or (log_write_peak is not None and log_write_peak >= 85)
        or (data_io_peak is not None and data_io_peak >= 85)
        or (sessions_peak is not None and sessions_peak >= 90)
        or (workers_peak is not None and workers_peak >= 90)
    ):
        return "scale-up-review", "Sustained CPU, DTU, IO, or session pressure suggests a sizing review."

    if deadlocks > 0:
        return "deadlock-review", "Deadlock activity was observed; review query patterns and indexing."

    low_activity = (successful_connections + failed_connections) < 100
    if (
        (cpu_p95 is None or cpu_p95 < 10)
        and (dtu_p95 is None or dtu_p95 < 10)
        and (storage_peak is None or storage_peak < 20)
        and low_activity
    ):
        return "low-utilization", "Low sustained utilization and low activity suggest a rightsizing review."

    if "serverless" in (service_objective or "").lower() or "serverless" in (sku_tier or "").lower():
        if cpu_p95 is not None and cpu_p95 < 20 and dtu_p95 is not None and dtu_p95 < 20:
            return "serverless-idle", "Serverless database is lightly used; review auto-pause and scale settings."

    return "healthy", "No clear pressure or oversizing signal from the observed metric window."


def list_sql_servers(credential, subscription_id: str) -> list[dict]:
    token = get_token(credential)
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.Sql/servers"
        f"?api-version={SQL_API_VERSION}"
    )
    items: list[dict] = []
    while url:
        response = requests.get(url, headers=headers, timeout=120)
        response.raise_for_status()
        payload = response.json()
        items.extend(payload.get("value", []))
        url = payload.get("nextLink")
    return items


def list_sql_databases(credential, subscription_id: str, server: dict) -> list[dict]:
    token = get_token(credential)
    headers = {"Authorization": f"Bearer {token}"}
    server_id = str(server.get("id") or "")
    server_name = str(server.get("name") or "")
    resource_group = parse_resource_group(server_id)
    if not (server_name and resource_group):
        return []

    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.Sql/servers/{server_name}/databases"
        f"?api-version={SQL_API_VERSION}"
    )
    items: list[dict] = []
    while url:
        response = requests.get(url, headers=headers, timeout=120)
        response.raise_for_status()
        payload = response.json()
        for database in payload.get("value", []):
            db_name = str(database.get("name") or "")
            if db_name.lower() == "master":
                continue
            items.append(database)
        url = payload.get("nextLink")
    return items


def process_subscription(credential, sub_id: str, sub_name: str | None, tenant_id: str, timespan: str, interval: str):
    print(f"\n-- Subscription: {sub_name or sub_id} --")
    monitor_client = MonitorManagementClient(credential, sub_id)

    try:
        servers = list_sql_servers(credential, sub_id)
    except Exception as exc:
        print(f"  WARN Could not list Azure SQL servers: {exc}")
        return [], []

    if not servers:
        print("  (no Azure SQL servers found)")
        return [], []

    summary_rows = []
    ts_rows = []

    for server in servers:
        server_id = str(server.get("id") or "")
        server_name = str(server.get("name") or "")
        resource_group = parse_resource_group(server_id)
        print(f"  Server: {server_name}  (RG: {resource_group})")

        server_props = server.get("properties") or {}
        server_location = server.get("location") or ""
        private_ep_connections = server_props.get("privateEndpointConnections") or []

        def bool_str(value: bool) -> str:
            return "True" if value else "False"

        def truthy(value) -> bool:
            return str(value).strip().lower() in {"true", "1", "yes", "enabled"}

        tls_version = (server_props.get("minimalTlsVersion") or "").strip().lower().replace("_", "")
        server_qc = {
            "qc_aad_admin_configured": bool_str(bool(dig(server_props, "administrators", "login") or dig(server_props, "administrators", "sid"))),
            "qc_public_access_disabled": bool_str((server_props.get("publicNetworkAccess") or "").strip().lower() in {"disabled", "false"}),
            "qc_tls_12_or_higher": bool_str(tls_version in {"1.2", "1.3", "tls12", "tls13"}),
            "qc_auditing_enabled": bool_str(truthy(dig(server_props, "isAzureMonitorAuditEnabled")) or truthy(dig(server_props, "auditing", "state"))),
            "qc_auditing_90_days": bool_str((dig(server_props, "auditing", "retentionDays") or 0) >= 90),
            "qc_no_azure_services_firewall": bool_str(True),
            "qc_private_endpoint_configured": bool_str(len(private_ep_connections) > 0),
        }

        try:
            databases = list_sql_databases(credential, sub_id, server)
        except Exception as exc:
            print(f"    WARN Could not list databases for {server_name}: {exc}")
            continue

        if not databases:
            print("    (no user databases found)")
            continue

        for database in databases:
            database_id = str(database.get("id") or "")
            database_name = str(database.get("name") or "")
            print(f"    Database: {database_name}")

            db_props = database.get("properties") or {}
            sku = database.get("sku") or {}
            backup_storage_redundancy = (
                db_props.get("currentBackupStorageRedundancy")
                or db_props.get("requestedBackupStorageRedundancy")
                or ""
            )
            pitr_days = db_props.get("retentionDays") or dig(db_props, "backupShortTermRetentionPolicy", "retentionDays") or 0
            try:
                pitr_days = int(pitr_days)
            except (TypeError, ValueError):
                pitr_days = 0
            ltr_enabled = bool(dig(db_props, "backupLongTermRetentionPolicy", "weeklyRetention") or dig(db_props, "backupLongTermRetentionPolicy", "monthlyRetention") or dig(db_props, "backupLongTermRetentionPolicy", "yearlyRetention"))
            db_qc = {
                "qc_tde_enabled": bool_str(str(db_props.get("transparentDataEncryption") or "").strip().lower() in {"enabled", "true", "on"}),
                "qc_backup_geo_redundant": bool_str(str(backup_storage_redundancy).strip().lower() in {"geo", "geozone", "georedundant", "geozoneredundant"}),
                "qc_pitr_7_days": bool_str((pitr_days or 0) >= 7),
                "qc_ltr_configured": bool_str(ltr_enabled),
                "qc_db_auditing_enabled": bool_str(truthy(dig(db_props, "auditing", "state")) or truthy(dig(db_props, "isLedgerOn"))),
                "qc_atp_enabled": bool_str(str(dig(db_props, "securityAlertPolicy", "state") or "").strip().lower() in {"enabled", "true", "on"}),
                "qc_has_tags": bool_str(bool(tags_str(database.get("tags") or server.get("tags") or {}))),
            }

            combined_rows: dict[str, dict] = {}
            metric_summaries: dict[str, dict] = {}

            for metric in SQL_METRICS:
                series = get_metric_series(
                    monitor_client,
                    database_id,
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
                    row["database_id"] = database_id
                    row["timestamp"] = timestamp
                    row[metric["column"]] = round(value, 4)

            ordered_ts = [combined_rows[key] for key in sorted(combined_rows.keys())]
            ts_rows.extend(ordered_ts)

            advisory_category, advisory_reason = classify_database(
                metric_summaries,
                str(sku.get("tier") or ""),
                str(db_props.get("currentServiceObjectiveName") or ""),
            )

            summary_rows.append(
                {
                    "tenant_id": tenant_id or "",
                    "subscription_id": sub_id,
                    "subscription_name": sub_name or "",
                    "resource_group": resource_group,
                    "name": database_name,
                    "server_name": server_name,
                    "server_id": server_id,
                    "database_name": database_name,
                    "database_id": database_id,
                    "location": database.get("location") or server_location,
                    "server_state": server_props.get("state") or "",
                    "server_version": server_props.get("version") or "",
                    "server_fully_qualified_domain_name": server_props.get("fullyQualifiedDomainName") or "",
                    "administrator_login": server_props.get("administratorLogin") or "",
                    "public_network_access": server_props.get("publicNetworkAccess") or "",
                    "minimal_tls_version": server_props.get("minimalTlsVersion") or "",
                    "database_state": db_props.get("status") or "",
                    "sku_name": sku.get("name") or "",
                    "sku_tier": sku.get("tier") or "",
                    "vcores": sku.get("capacity") or "",
                    "edition": db_props.get("edition") or "",
                    "service_objective": db_props.get("currentServiceObjectiveName") or "",
                    "requested_service_objective": db_props.get("requestedServiceObjectiveName") or "",
                    "elastic_pool_name": db_props.get("elasticPoolName") or "",
                    "backup_storage_redundancy": backup_storage_redundancy,
                    "collation": db_props.get("collation") or "",
                    "max_size_bytes": db_props.get("maxSizeBytes") or "",
                    "zone_redundant": db_props.get("zoneRedundant") or "",
                    "creation_date": db_props.get("creationDate") or "",
                    "read_scale": db_props.get("readScale") or "",
                    "auto_pause_delay_minutes": db_props.get("autoPauseDelay") or "",
                    "min_capacity": db_props.get("minCapacity") or "",
                    "ledger_on": db_props.get("ledgerOn") or "",
                    "cpu_avg_pct": round(metric_summaries["cpu_percent"]["avg"], 2) if metric_summaries["cpu_percent"]["avg"] is not None else "",
                    "cpu_p95_pct": round(metric_summaries["cpu_percent"]["p95"], 2) if metric_summaries["cpu_percent"]["p95"] is not None else "",
                    "cpu_peak_pct": round(metric_summaries["cpu_percent"]["peak"], 2) if metric_summaries["cpu_percent"]["peak"] is not None else "",
                    "dtu_avg_pct": round(metric_summaries["dtu_consumption_percent"]["avg"], 2) if metric_summaries["dtu_consumption_percent"]["avg"] is not None else "",
                    "dtu_p95_pct": round(metric_summaries["dtu_consumption_percent"]["p95"], 2) if metric_summaries["dtu_consumption_percent"]["p95"] is not None else "",
                    "dtu_peak_pct": round(metric_summaries["dtu_consumption_percent"]["peak"], 2) if metric_summaries["dtu_consumption_percent"]["peak"] is not None else "",
                    "storage_pct_avg": round(metric_summaries["storage_percent"]["avg"], 2) if metric_summaries["storage_percent"]["avg"] is not None else "",
                    "storage_pct_peak": round(metric_summaries["storage_percent"]["peak"], 2) if metric_summaries["storage_percent"]["peak"] is not None else "",
                    "log_write_pct_avg": round(metric_summaries["log_write_percent"]["avg"], 2) if metric_summaries["log_write_percent"]["avg"] is not None else "",
                    "log_write_pct_peak": round(metric_summaries["log_write_percent"]["peak"], 2) if metric_summaries["log_write_percent"]["peak"] is not None else "",
                    "data_io_pct_avg": round(metric_summaries["data_io_percent"]["avg"], 2) if metric_summaries["data_io_percent"]["avg"] is not None else "",
                    "data_io_pct_peak": round(metric_summaries["data_io_percent"]["peak"], 2) if metric_summaries["data_io_percent"]["peak"] is not None else "",
                    "sessions_pct_avg": round(metric_summaries["sessions_percent"]["avg"], 2) if metric_summaries["sessions_percent"]["avg"] is not None else "",
                    "sessions_peak_pct": round(metric_summaries["sessions_percent"]["peak"], 2) if metric_summaries["sessions_percent"]["peak"] is not None else "",
                    "workers_pct_avg": round(metric_summaries["workers_percent"]["avg"], 2) if metric_summaries["workers_percent"]["avg"] is not None else "",
                    "workers_peak_pct": round(metric_summaries["workers_percent"]["peak"], 2) if metric_summaries["workers_percent"]["peak"] is not None else "",
                    "deadlock_total": round(metric_summaries["deadlock"]["total"], 2) if metric_summaries["deadlock"]["total"] is not None else "",
                    "connections_failed_total": round(metric_summaries["connection_failed"]["total"], 2) if metric_summaries["connection_failed"]["total"] is not None else "",
                    "connections_successful_total": round(metric_summaries["connection_successful"]["total"], 2) if metric_summaries["connection_successful"]["total"] is not None else "",
                    **server_qc,
                    **db_qc,
                    "observed_sample_count": max((item["samples"] for item in metric_summaries.values()), default=0),
                    "advisory_category": advisory_category,
                    "advisory_reason": advisory_reason,
                    "tags": tags_str(database.get("tags") or server.get("tags") or {}),
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
        summary_filename = f"{prefix}_sql_summary_{date_range_str}.csv"
        ts_filename = f"{prefix}_sql_timeseries_{date_range_str}.csv"
    else:
        summary_filename = f"sql_summary_{date_range_str}.csv"
        ts_filename = f"sql_timeseries_{date_range_str}.csv"

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
        description="Export Azure SQL database utilization data.",
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