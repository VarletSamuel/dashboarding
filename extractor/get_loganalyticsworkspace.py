#!/usr/bin/env python3
"""
Log Analytics Workspace - Daily Caps & Retention Exporter
===========================================================
Exports Log Analytics workspace configuration, daily caps, retention policies,
and telemetry usage data into two files that fit the existing dashboarding/reporting
contract used in this repo:

  *_loganalyticsworkspace_summary_*.csv
      One row per workspace with configuration, daily cap, retention period,
      current month usage, and cost insights.

  *_loganalyticsworkspace_timeseries_*.csv
      One row per (workspace, day) with daily ingestion volume for trend analysis
      and capacity planning visualization.

Output: semicolon-delimited, BOM-prefixed CSV (Excel-compatible).

Prerequisites
-------------
    pip install azure-identity azure-mgmt-loganalytics requests

Authentication
--------------
    Same contract as the other extractors in this repo: reads the customer
    JSON (-i), groups subscriptions by tenant, reuses the existing az session
    (unless --skip-login is omitted and the wrapper logs in first), and then
    iterates the matching subscriptions.

Usage
-----
    python get_loganalyticsworkspace.py -i ../customers/CUST.json --skip-login --output-dir ./reports/CUST
    python get_loganalyticsworkspace.py -i ../customers/CUST.json --from 2026-01-01 --to 2026-03-31 --output-dir ./reports/CUST
    python get_loganalyticsworkspace.py -s <subscription-id> --lookback PT30D --output-dir ./reports
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
    "azure.mgmt.loganalytics": "azure-mgmt-loganalytics",
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
from azure.mgmt.loganalytics import LogAnalyticsManagementClient  # noqa: E402


SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "workspace_name",
    "workspace_id",
    "location",
    "workspace_state",
    "sku_name",
    "daily_quota_gb",
    "daily_quota_exceeded",
    "retention_days",
    "retention_in_days_4d_option",
    "managed_by_template",
    "features_search_version",
    "features_legacy",
    "features_unlimited_schema",
    "ingestion_gb_month_to_date",
    "ingestion_gb_last_7d_avg",
    "estimated_monthly_cost",
    "created_date",
    "tags",
]

TIMESERIES_COLUMNS = [
    "workspace_id",
    "date",
    "ingestion_gb",
    "search_query_count",
]

LAW_API_VERSION = "2021-12-01-preview"


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


def read_customer_json(json_path: str) -> dict:
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


def list_law_workspaces(credential, subscription_id: str) -> list[dict]:
    token = get_token(credential)
    headers = {"Authorization": f"Bearer {token}"}
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.OperationalInsights/workspaces"
        f"?api-version={LAW_API_VERSION}"
    )
    items: list[dict] = []
    while url:
        response = requests.get(url, headers=headers, timeout=120)
        response.raise_for_status()
        payload = response.json()
        items.extend(payload.get("value", []))
        url = payload.get("nextLink")
    return items


def query_law_usage(credential, subscription_id: str, workspace_name: str, resource_group: str, start: datetime, end: datetime) -> list[dict]:
    """Query Log Analytics usage data via analytics query."""
    from azure.mgmt.loganalytics import LogAnalyticsManagementClient

    client = LogAnalyticsManagementClient(credential, subscription_id)
    
    # Format dates for KQL query
    start_str = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    
    # KQL query to get daily ingestion by table
    query = f"""
Usage
| where TimeGenerated between (datetime('{start_str}') .. datetime('{end_str}'))
| summarize 
    TotalGb = sum(Quantity) / 1000.0,
    QueryCount = dcount(RequestId)
    by bin(TimeGenerated, 1d)
| sort by TimeGenerated asc
"""

    try:
        response = client.query.execute(
            resource_group_name=resource_group,
            workspace_name=workspace_name,
            body={"query": query}
        )
        return response.tables[0].rows if response.tables else []
    except Exception as exc:
        print(f"    WARN Could not query usage for {workspace_name}: {exc}")
        return []


def get_law_usage_stats(credential, subscription_id: str, workspace_name: str, resource_group: str, start: datetime, end: datetime) -> dict:
    """Get aggregated usage statistics."""
    stats = {
        "total_gb": 0.0,
        "avg_daily_gb": 0.0,
        "peak_daily_gb": 0.0,
        "query_count": 0,
        "days_with_data": 0,
    }
    
    try:
        usage_rows = query_law_usage(credential, subscription_id, workspace_name, resource_group, start, end)
        if not usage_rows:
            return stats
        
        daily_values = []
        query_counts = []
        
        for row in usage_rows:
            gb = float(row[1]) if len(row) > 1 else 0.0
            queries = int(row[2]) if len(row) > 2 else 0
            daily_values.append(gb)
            query_counts.append(queries)
        
        stats["total_gb"] = round(sum(daily_values), 4)
        stats["days_with_data"] = len([v for v in daily_values if v > 0])
        if stats["days_with_data"] > 0:
            stats["avg_daily_gb"] = round(stats["total_gb"] / stats["days_with_data"], 4)
        stats["peak_daily_gb"] = round(max(daily_values), 4) if daily_values else 0.0
        stats["query_count"] = sum(query_counts)
    except Exception as exc:
        print(f"    WARN Could not compute usage stats: {exc}")
    
    return stats


def estimate_monthly_cost(daily_quota_gb: float | None, ingestion_gb: float) -> float:
    """Rough estimate of Log Analytics cost based on ingestion."""
    if daily_quota_gb and daily_quota_gb > 0:
        if ingestion_gb <= daily_quota_gb:
            # Within daily quota, usually included in base pricing
            return 0.0
    
    # Estimate: ~$2.76/GB for data beyond quota (varies by region/commitment tier)
    # This is a rough estimate; actual pricing varies
    overage = max(0, ingestion_gb - (daily_quota_gb or 0))
    return round(overage * 2.76, 2)


def process_subscription(credential, sub_id: str, sub_name: str | None, tenant_id: str, start: datetime, end: datetime):
    print(f"\n-- Subscription: {sub_name or sub_id} --")

    try:
        workspaces = list_law_workspaces(credential, sub_id)
    except Exception as exc:
        print(f"  WARN Could not list Log Analytics workspaces: {exc}")
        return [], []

    if not workspaces:
        print("  (no Log Analytics workspaces found)")
        return [], []

    summary_rows = []
    ts_rows = []

    for workspace in workspaces:
        workspace_id = str(workspace.get("id") or "")
        workspace_name = str(workspace.get("name") or "")
        resource_group = parse_resource_group(workspace_id)
        
        print(f"  Workspace: {workspace_name}  (RG: {resource_group})")

        workspace_props = workspace.get("properties") or {}
        workspace_location = workspace.get("location") or ""

        # Extract configuration
        daily_quota_gb = dig(workspace_props, "workspaceCapping", "dailyQuotaGb")
        retention_days = dig(workspace_props, "retentionInDays") or 30
        provisioning_state = workspace_props.get("provisioningState") or "Unknown"
        sku = dig(workspace, "sku") or {}
        sku_name = sku.get("name") or "unspecified"

        # Get usage statistics
        usage_stats = get_law_usage_stats(credential, sub_id, workspace_name, resource_group, start, end)

        # Check if daily quota has been exceeded
        daily_quota_exceeded = False
        if daily_quota_gb and daily_quota_gb > 0:
            # If peak daily ingestion exceeded quota
            daily_quota_exceeded = usage_stats["peak_daily_gb"] > daily_quota_gb

        # Estimate monthly cost (extrapolate from period)
        period_days = (end - start).days or 1
        projected_monthly_gb = usage_stats["avg_daily_gb"] * 30 if usage_stats["days_with_data"] > 0 else 0
        estimated_cost = estimate_monthly_cost(daily_quota_gb, projected_monthly_gb)

        # Build summary row
        summary_rows.append(
            {
                "tenant_id": tenant_id,
                "subscription_id": sub_id,
                "subscription_name": sub_name or "",
                "resource_group": resource_group,
                "workspace_name": workspace_name,
                "workspace_id": workspace_id,
                "location": workspace_location,
                "workspace_state": provisioning_state,
                "sku_name": sku_name,
                "daily_quota_gb": daily_quota_gb or "Unlimited",
                "daily_quota_exceeded": "True" if daily_quota_exceeded else "False",
                "retention_days": retention_days or "",
                "retention_in_days_4d_option": dig(workspace_props, "retentionInDays4d") or "",
                "managed_by_template": dig(workspace_props, "publicNetworkAccessForIngestion") or "",
                "features_search_version": dig(workspace_props, "features", "searchVersion") or "",
                "features_legacy": dig(workspace_props, "features", "legacy") or "",
                "features_unlimited_schema": dig(workspace_props, "features", "enableLogAccessUsingOnlyResourcePermissions") or "",
                "ingestion_gb_month_to_date": round(usage_stats["total_gb"], 4),
                "ingestion_gb_last_7d_avg": round(usage_stats["avg_daily_gb"], 4),
                "estimated_monthly_cost": estimated_cost,
                "created_date": workspace_props.get("createdDate") or "",
                "tags": tags_str(workspace.get("tags") or {}),
            }
        )

        # Build timeseries rows (if we have usage data)
        try:
            usage_rows = query_law_usage(credential, sub_id, workspace_name, resource_group, start, end)
            for row in usage_rows:
                timestamp_str = row[0] if len(row) > 0 else ""
                gb = float(row[1]) if len(row) > 1 else 0.0
                queries = int(row[2]) if len(row) > 2 else 0
                
                if timestamp_str:
                    # Parse timestamp
                    try:
                        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
                        date_str = ts.strftime("%Y-%m-%d")
                    except:
                        date_str = timestamp_str[:10]
                    
                    ts_rows.append(
                        {
                            "workspace_id": workspace_id,
                            "date": date_str,
                            "ingestion_gb": round(gb, 4),
                            "search_query_count": queries,
                        }
                    )
        except Exception as exc:
            print(f"    WARN Could not build timeseries for {workspace_name}: {exc}")

    return summary_rows, ts_rows


def export(args):
    now = datetime.now(timezone.utc)

    if args.date_from:
        start = parse_date(args.date_from)
        end = parse_date(args.date_to) if args.date_to else now
    elif args.lookback:
        lookback = parse_lookback(args.lookback)
        end = now
        start = end - lookback
    else:
        # Default: last 7 days
        end = now
        start = end - timedelta(days=7)

    print(f"Date range: {start.date()} to {end.date()}")

    all_summary_rows = []
    all_ts_rows = []

    if args.subscription:
        # Single subscription mode
        cred = get_credential(
            sp_client_id=args.sp_client_id,
            sp_client_secret=args.sp_client_secret,
            sp_certificate=args.sp_certificate,
        )
        subs = list_enabled_subscriptions(cred)
        target_subs = [
            (sub_id, sub_name, tenant_id)
            for sub_id, sub_name, tenant_id in subs
            if sub_id == args.subscription
        ]

        if not target_subs:
            print(f"ERROR: Subscription '{args.subscription}' not found in credential scope")
            return

        for sub_id, sub_name, tenant_id in target_subs:
            summary, ts = process_subscription(cred, sub_id, sub_name, tenant_id, start, end)
            all_summary_rows.extend(summary)
            all_ts_rows.extend(ts)
    elif args.input:
        # Multi-tenant mode from customer JSON
        tenant_map = read_customer_json(args.input)
        for tenant_id, subs in tenant_map.items():
            print(f"\nTenant: {tenant_id}")
            cred = get_credential(
                tenant_id=tenant_id,
                sp_client_id=args.sp_client_id,
                sp_client_secret=args.sp_client_secret,
                sp_certificate=args.sp_certificate,
            )

            for sub_id, sub_name in subs:
                summary, ts = process_subscription(cred, sub_id, sub_name, tenant_id, start, end)
                all_summary_rows.extend(summary)
                all_ts_rows.extend(ts)
    else:
        # Scan all subscriptions
        cred = get_credential(
            sp_client_id=args.sp_client_id,
            sp_client_secret=args.sp_client_secret,
            sp_certificate=args.sp_certificate,
        )
        subs = list_enabled_subscriptions(cred)
        for sub_id, sub_name, tenant_id in subs:
            summary, ts = process_subscription(cred, sub_id, sub_name, tenant_id, start, end)
            all_summary_rows.extend(summary)
            all_ts_rows.extend(ts)

    # Write outputs
    os.makedirs(args.output_dir, exist_ok=True)
    date_range_str = f"{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}"

    if args.output_format in ("csv", "both"):
        summary_csv_path = os.path.join(args.output_dir, f"loganalyticsworkspace_summary_{date_range_str}.csv")
        with open(summary_csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            writer.writeheader()
            writer.writerows(all_summary_rows)
        print(f"  -> {summary_csv_path}  ({len(all_summary_rows)} workspace(s))")

        if all_ts_rows:
            ts_csv_path = os.path.join(args.output_dir, f"loganalyticsworkspace_timeseries_{date_range_str}.csv")
            with open(ts_csv_path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=TIMESERIES_COLUMNS, delimiter=";", quoting=csv.QUOTE_MINIMAL)
                writer.writeheader()
                writer.writerows(all_ts_rows)
            print(f"  -> {ts_csv_path}  ({len(all_ts_rows)} data point(s))")

    if args.output_format in ("json", "both"):
        summary_json_path = os.path.join(args.output_dir, f"loganalyticsworkspace_summary_{date_range_str}.json")
        with open(summary_json_path, "w", encoding="utf-8") as f:
            json.dump(all_summary_rows, f, indent=2, default=str)
        print(f"  -> {summary_json_path}")

        if all_ts_rows:
            ts_json_path = os.path.join(args.output_dir, f"loganalyticsworkspace_timeseries_{date_range_str}.json")
            with open(ts_json_path, "w", encoding="utf-8") as f:
                json.dump(all_ts_rows, f, indent=2, default=str)
            print(f"  -> {ts_json_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Export Log Analytics workspace configuration, daily caps, retention, and telemetry usage.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
    %(prog)s -i ../customers/CUST.json --skip-login --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.json --from 2026-01-01 --to 2026-03-31 --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.json --lookback PT30D --output-dir ./reports/CUST
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
        help="Usage lookback window: minutes (int) or ISO duration like PT30D (default: last 7 days).",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Start date for usage query (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS).",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="End date for usage query (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). Defaults to now when omitted.",
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
