#!/usr/bin/env python3
"""
Fetch likely orphaned Azure resources that still cost money.

Output (1 CSV file per run):
  orphaned_costly_resources_<customer-or-sub>_<from>_<to>.csv

Two modes of operation:
  Single subscription:  -s <subscription-id>
  Customer batch:       -c <customer-code> -i <customers.csv>

Prerequisites:
    pip install azure-identity requests
    az login  (must have an active session; batch mode can re-login per tenant)

Permissions:
    Reader + Cost Management Reader on each subscription

Usage:
    python get_orphaned_resources.py -s <subscription-id>
    python get_orphaned_resources.py -s <subscription-id> --min-cost 100
    python get_orphaned_resources.py -c CUST -i customers.csv --skip-login
    python get_orphaned_resources.py -c CUST -i customers.csv --from 2026-03-01 --to 2026-03-31

Notes:
  - Orphan detection is heuristic-based and uses Azure Resource Graph.
  - This script is read-only and does not delete or modify resources.
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# Dependency check
_REQUIRED = {
    "azure.identity": "azure-identity",
    "requests": "requests",
}
_missing = []
for _mod, _pkg in _REQUIRED.items():
    try:
        __import__(_mod)
    except ImportError:
        _missing.append(_pkg)

if _missing:
    print("ERROR: Missing packages:", ", ".join(_missing))
    print(f"\n    {sys.executable} -m pip install {' '.join(_missing)}\n")
    sys.exit(1)

import requests
from azure.identity import (
    AzureCliCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
)

CSV_COLUMNS = [
    "subscription_id",
    "subscription_name",
    "resource_group",
    "resource_name",
    "resource_id",
    "resource_type",
    "orphan_reason",
    "location",
    "cost",
    "cost_usd",
    "currency",
    "period_from",
    "period_to",
]


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


def read_customers(path: str) -> list[dict]:
    """Read the customer JSON file and return all subscription rows."""
    if not os.path.exists(path):
        print(f"ERROR: Customer file not found: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    subs = []
    for entry in data.get("azure", []):
        sub_id = (entry.get("subscription_id") or "").strip()
        tenant_id = (entry.get("tenant_id") or "").strip()
        sub_name = (entry.get("subscription_name") or "").strip()
        if sub_id and tenant_id:
            subs.append(
                {
                    "tenant_id": tenant_id,
                    "subscription_id": sub_id,
                    "subscription_name": sub_name,
                }
            )
    return subs


def _retry_after_seconds(resp: requests.Response, default_seconds: int = 30) -> int:
    values = []

    retry_after = resp.headers.get("Retry-After")
    if retry_after and retry_after.isdigit():
        values.append(int(retry_after))

    ratelimit_headers = [
        "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-tenant-retry-after",
    ]
    for header_name in ratelimit_headers:
        value = resp.headers.get(header_name)
        if value and value.isdigit():
            values.append(int(value))

    return max(values) if values else default_seconds


def query_cost_by_resource(
    token: str,
    subscription_id: str,
    date_from: date,
    date_to: date,
) -> dict[str, dict]:
    """
    Query Cost Management API (daily by resource) and aggregate totals per resource id.
    Returns map keyed by lower(resource_id).
    """
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        "/providers/Microsoft.CostManagement/query"
        "?api-version=2023-03-01&$top=5000"
    )

    payload = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {
                "totalCost": {"name": "Cost", "function": "Sum"},
                "totalCostUSD": {"name": "CostUSD", "function": "Sum"},
            },
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
                {"type": "Dimension", "name": "ResourceType"},
            ],
        },
    }

    # Compatibility fallback for tenants where ResourceType grouping is rejected.
    fallback_payload = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
        },
        "dataset": {
            "granularity": "Daily",
            "aggregation": {
                "totalCost": {"name": "Cost", "function": "Sum"},
                "totalCostUSD": {"name": "CostUSD", "function": "Sum"},
            },
            "grouping": [
                {"type": "Dimension", "name": "ResourceId"},
            ],
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "ClientType": "GitHubCopilotForAzure",
    }

    totals: dict[str, dict] = {}
    next_url = url
    current_payload = payload
    used_fallback = False

    while next_url:
        resp = requests.post(next_url, json=current_payload, headers=headers, timeout=120)
        if resp.status_code == 429:
            wait_seconds = _retry_after_seconds(resp)
            print(f"      Cost API throttled - waiting {wait_seconds}s ...", flush=True)
            time.sleep(wait_seconds)
            continue

        if resp.status_code == 400 and not used_fallback:
            # Retry once with a simpler query shape.
            used_fallback = True
            next_url = url
            current_payload = fallback_payload
            print("      Cost API returned 400 - retrying with simplified grouping ...", flush=True)
            continue

        if resp.status_code == 400:
            detail = (resp.text or "").strip().replace("\n", " ")
            raise requests.exceptions.HTTPError(
                f"Cost API 400: {detail[:500]}",
                response=resp,
            )

        resp.raise_for_status()
        data = resp.json().get("properties", {})

        columns = data.get("columns", [])
        rows = data.get("rows", [])

        idx = {c.get("name", ""): i for i, c in enumerate(columns)}
        i_cost = idx.get("totalCost")
        i_cost_usd = idx.get("totalCostUSD")
        i_resource_id = idx.get("ResourceId")
        i_resource_type = idx.get("ResourceType")
        i_currency = idx.get("Currency")

        for row in rows:
            if i_resource_id is None or i_resource_id >= len(row):
                continue

            resource_id = str(row[i_resource_id] or "").strip()
            if not resource_id:
                continue

            key = resource_id.lower()
            if key not in totals:
                totals[key] = {
                    "resource_id": resource_id,
                    "resource_type": (
                        str(row[i_resource_type])
                        if i_resource_type is not None and i_resource_type < len(row)
                        else ""
                    ),
                    "cost": 0.0,
                    "cost_usd": 0.0,
                    "currency": (
                        str(row[i_currency])
                        if i_currency is not None and i_currency < len(row)
                        else ""
                    ),
                }

            if i_cost is not None and i_cost < len(row) and row[i_cost] is not None:
                totals[key]["cost"] += float(row[i_cost])
            if i_cost_usd is not None and i_cost_usd < len(row) and row[i_cost_usd] is not None:
                totals[key]["cost_usd"] += float(row[i_cost_usd])

        next_url = data.get("nextLink")
        current_payload = payload

    return totals


def query_orphan_candidates(token: str, subscription_id: str) -> list[dict]:
    """
    Query Azure Resource Graph for common orphan patterns.
    Returns resource rows with reason labels.
    """
    url = "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=2022-10-01"

    query = r"""
let orphanDisks =
Resources
| where type =~ 'microsoft.compute/disks'
| extend managedBy = tostring(managedBy), diskState = tostring(properties.diskState)
| where isempty(managedBy) or diskState =~ 'Unattached'
| project id, name, type, resourceGroup, location, orphan_reason = 'Unattached managed disk';
let orphanPublicIps =
Resources
| where type =~ 'microsoft.network/publicipaddresses'
| extend ipConfigId = tostring(properties.ipConfiguration.id)
| where isempty(ipConfigId)
| project id, name, type, resourceGroup, location, orphan_reason = 'Public IP not associated';
let orphanNics =
Resources
| where type =~ 'microsoft.network/networkinterfaces'
| extend vmId = tostring(properties.virtualMachine.id)
| where isempty(vmId)
| project id, name, type, resourceGroup, location, orphan_reason = 'NIC not attached to VM';
let orphanNsgs =
Resources
| where type =~ 'microsoft.network/networksecuritygroups'
| extend subnetCount = array_length(properties.subnets), nicCount = array_length(properties.networkInterfaces)
| where coalesce(subnetCount, 0) == 0 and coalesce(nicCount, 0) == 0
| project id, name, type, resourceGroup, location, orphan_reason = 'NSG not associated to subnet or NIC';
let orphanRouteTables =
Resources
| where type =~ 'microsoft.network/routetables'
| extend subnetCount = array_length(properties.subnets)
| where coalesce(subnetCount, 0) == 0
| project id, name, type, resourceGroup, location, orphan_reason = 'Route table not associated to subnet';
let orphanNatGateways =
Resources
| where type =~ 'microsoft.network/natgateways'
| extend subnetCount = array_length(properties.subnets), pipCount = array_length(properties.publicIpAddresses)
| where coalesce(subnetCount, 0) == 0
| project id, name, type, resourceGroup, location, orphan_reason = 'NAT gateway not associated to subnet';
let appServicePlans =
Resources
| where type =~ 'microsoft.web/serverfarms'
| project planJoinId = tolower(id), id, name, type, resourceGroup, location;
let appServiceSites =
Resources
| where type =~ 'microsoft.web/sites'
| extend planJoinId = tolower(tostring(properties.serverFarmId))
| summarize appCount = count() by planJoinId;
let orphanAppServicePlans =
appServicePlans
| join kind=leftouter appServiceSites on planJoinId
| where coalesce(appCount, 0) == 0
| project id, name, type, resourceGroup, location, orphan_reason = 'App Service plan with no apps';
union orphanDisks, orphanPublicIps, orphanNics, orphanNsgs, orphanRouteTables, orphanNatGateways, orphanAppServicePlans
| summarize orphan_reason = any(orphan_reason), name = any(name), type = any(type), resourceGroup = any(resourceGroup), location = any(location) by id
| order by type asc, name asc
"""

    payload = {
        "subscriptions": [subscription_id],
        "query": query,
        "options": {"resultFormat": "objectArray", "$top": 5000},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    results = []
    skip_token = None

    while True:
        body = dict(payload)
        if skip_token:
            body["options"] = dict(payload["options"])
            body["options"]["skipToken"] = skip_token

        resp = requests.post(url, json=body, headers=headers, timeout=120)
        if resp.status_code == 429:
            wait_seconds = _retry_after_seconds(resp)
            print(f"      ARG throttled - waiting {wait_seconds}s ...", flush=True)
            time.sleep(wait_seconds)
            continue

        if resp.status_code == 400:
            detail = (resp.text or "").strip().replace("\n", " ")
            raise requests.exceptions.HTTPError(
                f"Resource Graph 400: {detail[:500]}",
                response=resp,
            )

        resp.raise_for_status()
        data = resp.json()
        chunk = data.get("data", [])
        if isinstance(chunk, list):
            results.extend(chunk)

        skip_token = data.get("$skipToken")
        if not skip_token:
            break

    return results


def fetch_orphaned_costly_resources(
    token: str,
    subscription_id: str,
    subscription_name: str,
    date_from: date,
    date_to: date,
    min_cost: float,
) -> list[dict] | None:
    """Return costly orphaned resource records for one subscription."""
    display = subscription_name or subscription_id
    short_id = subscription_id[:8]
    print(f"\n  -> {display} ({short_id}...)")

    try:
        orphan_rows = query_orphan_candidates(token, subscription_id)
        costs_by_resource = query_cost_by_resource(token, subscription_id, date_from, date_to)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        message = str(e)
        print(f"    ERROR: HTTP {status} - {message}")
        print("    Skipping this subscription")
        return None
    except Exception as e:
        print(f"    ERROR: {e} - skipping this subscription")
        return None

    print(f"    Orphan candidates found: {len(orphan_rows)}")

    records: list[dict] = []
    for orphan in orphan_rows:
        rid = str(orphan.get("id") or "").strip()
        if not rid:
            continue
        cost_info = costs_by_resource.get(rid.lower())
        if not cost_info:
            continue

        cost = float(cost_info.get("cost", 0.0) or 0.0)
        if cost < min_cost:
            continue

        records.append(
            {
                "subscription_id": subscription_id,
                "subscription_name": subscription_name,
                "resource_group": str(orphan.get("resourceGroup") or ""),
                "resource_name": str(orphan.get("name") or ""),
                "resource_id": rid,
                "resource_type": str(orphan.get("type") or cost_info.get("resource_type") or ""),
                "orphan_reason": str(orphan.get("orphan_reason") or "Likely orphaned"),
                "location": str(orphan.get("location") or ""),
                "cost": round(cost, 4),
                "cost_usd": round(float(cost_info.get("cost_usd", 0.0) or 0.0), 4),
                "currency": str(cost_info.get("currency") or ""),
                "period_from": date_from.isoformat(),
                "period_to": date_to.isoformat(),
            }
        )

    records.sort(key=lambda r: r["cost"], reverse=True)
    print(f"    Costly orphaned resources: {len(records)} (min cost {min_cost:.2f})")
    return records


def format_eu_number(value):
    """Format float with comma as decimal separator for European CSV output."""
    if isinstance(value, float):
        return f"{value:.4f}".replace(".", ",")
    return str(value)


def write_csv(path: Path, records: list[dict], fieldnames: list[str]):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for record in records:
            eu_record = {k: format_eu_number(v) for k, v in record.items()}
            writer.writerow(eu_record)


def write_json(path: Path, payload: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def write_output_csv(
    out_dir: Path,
    all_records: list[dict],
    label: str,
    date_from: date,
    date_to: date,
):
    suffix = f"{label}_orphaned_resources_{date_from}_{date_to}"
    path = out_dir / f"{suffix}.csv"
    write_csv(path, all_records, CSV_COLUMNS)
    print(f"\nCSV written: {path}")


def write_output_json(
    out_dir: Path,
    all_records: list[dict],
    label: str,
    date_from: date,
    date_to: date,
    min_cost: float,
):
    suffix = f"{label}_orphaned_resources_{date_from}_{date_to}"
    path = out_dir / f"{suffix}.json"
    write_json(
        path,
        {
            "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "min_cost": min_cost,
            "orphaned_costly_resources": all_records,
        },
    )
    print(f"JSON written: {path}")


def print_summary(records: list[dict], date_from: date, date_to: date, label: str = ""):
    width = 90
    total_cost = sum(r["cost"] for r in records)
    total_cost_usd = sum(r["cost_usd"] for r in records)
    currency = records[0]["currency"] if records else ""

    print(f"\n{'=' * width}")
    if label:
        print(f"  {label}")
    print(f"  Costly Orphaned Resource Report  |  {date_from} -> {date_to}")
    print(f"  Total likely waste: {total_cost:,.2f} {currency}  ({total_cost_usd:,.2f} USD)")
    print(f"  Resources flagged: {len(records)}")
    print(f"{'=' * width}")

    print(f"\n  Top 20 by cost")
    print(f"  {'Cost':>12}  {'Type':<45}  {'Name'}")
    print(f"  {'-' * 12}  {'-' * 45}  {'-' * 20}")
    for row in records[:20]:
        print(f"  {row['cost']:>12.2f}  {row['resource_type'][:45]:<45}  {row['resource_name']}")

    print(f"\n{'=' * width}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch likely orphaned Azure resources that cost more than a threshold."
    )

    parser.add_argument("-s", "--subscription", help="Single subscription ID.")

    parser.add_argument(
        "-i",
        "--input",
        help="Path to semicolon-delimited customer CSV (all rows processed).",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        help="Start date (YYYY-MM-DD). Default: first day of previous month.",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        help="End date (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--min-cost",
        type=float,
        default=100.0,
        help="Only keep likely orphaned resources whose period cost is >= this amount. Default: 100.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for output files. Default: current directory.",
    )
    parser.add_argument(
        "--output-format",
        choices=("csv", "json", "both"),
        default="both",
        help="File output format: csv, json, or both (default: both).",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Deprecated no-op; kept for backward compatibility.",
    )
    parser.add_argument(
        "--sp-client-id",
        default=None,
        metavar="APP_ID",
        help="App Registration client ID for non-interactive service principal auth.",
    )
    parser.add_argument(
        "--sp-client-secret",
        default=os.environ.get("AZURE_SP_CLIENT_SECRET"),
        metavar="SECRET",
        help="Client secret for service principal auth (fallback: AZURE_SP_CLIENT_SECRET).",
    )
    parser.add_argument(
        "--sp-certificate",
        default=None,
        metavar="CERT_PATH",
        help="Path to PEM certificate for service principal auth (alternative to --sp-client-secret).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also print JSON to stdout.",
    )
    args = parser.parse_args()

    if not args.subscription and not args.input:
        parser.error("Provide either -s/--subscription or -i/--input.")

    today = date.today()
    first_of_this_month = today.replace(day=1)
    default_from = (first_of_this_month - timedelta(days=1)).replace(day=1)

    date_from = date.fromisoformat(args.date_from) if args.date_from else default_from
    date_to = date.fromisoformat(args.date_to) if args.date_to else today

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Period: {date_from} -> {date_to}")
    print(f"Min cost filter: {args.min_cost:.2f}")

    all_records: list[dict] = []

    if args.subscription:
        credential = get_credential(
            None,
            args.sp_client_id,
            args.sp_client_secret,
            args.sp_certificate,
        )
        token = get_token(credential)
        sub_id = args.subscription
        records = fetch_orphaned_costly_resources(
            token,
            sub_id,
            sub_id,
            date_from,
            date_to,
            args.min_cost,
        )
        if records is None:
            sys.exit(1)

        all_records = records

        if args.output_format in ("csv", "both"):
            write_output_csv(out_dir, all_records, sub_id[:8], date_from, date_to)
        if args.output_format in ("json", "both"):
            write_output_json(out_dir, all_records, sub_id[:8], date_from, date_to, args.min_cost)

        if args.json:
            print(
                json.dumps(
                    {
                        "subscription_id": sub_id,
                        "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
                        "min_cost": args.min_cost,
                        "orphaned_costly_resources": all_records,
                    },
                    indent=2,
                )
            )
        else:
            print_summary(all_records, date_from, date_to)
        return

    subscriptions = read_customers(args.input)

    if not subscriptions:
        print(f"ERROR: No subscriptions found in {args.input}.", file=sys.stderr)
        sys.exit(1)

    csv_label = Path(args.input).stem.upper()
    print(f"CSV: {args.input}")
    print(f"Subscriptions: {len(subscriptions)}")

    by_tenant: dict[str, list[dict]] = defaultdict(list)
    for sub in subscriptions:
        by_tenant[sub["tenant_id"]].append(sub)

    success = 0
    errors: list[str] = []

    for tenant_id, tenant_subs in by_tenant.items():
        try:
            credential = get_credential(
                tenant_id,
                args.sp_client_id,
                args.sp_client_secret,
                args.sp_certificate,
            )
            token = get_token(credential)
        except Exception as e:
            msg = f"Failed to create credential/token for tenant {tenant_id}: {e}"
            print(f"  ! {msg}")
            errors.append(msg)
            continue

        for sub in tenant_subs:
            records = fetch_orphaned_costly_resources(
                token,
                sub["subscription_id"],
                sub["subscription_name"],
                date_from,
                date_to,
                args.min_cost,
            )
            if records is None:
                errors.append(f"{sub['subscription_name']} ({sub['subscription_id'][:8]}...)")
                continue

            all_records.extend(records)
            success += 1

    all_records.sort(key=lambda r: r["cost"], reverse=True)

    if args.output_format in ("csv", "both"):
        write_output_csv(out_dir, all_records, csv_label, date_from, date_to)
    if args.output_format in ("json", "both"):
        write_output_json(out_dir, all_records, csv_label, date_from, date_to, args.min_cost)

    if args.json:
        print(
            json.dumps(
                {
                    "input_csv": args.input,
                    "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
                    "min_cost": args.min_cost,
                    "orphaned_costly_resources": all_records,
                },
                indent=2,
            )
        )
    else:
        print_summary(
            all_records,
            date_from,
            date_to,
            f"CSV: {csv_label}  ({success} subscriptions)",
        )

    print(f"\n{'=' * 70}")
    print(f"  Done. {success}/{len(subscriptions)} subscriptions processed successfully.")
    if errors:
        print(f"  ! {len(errors)} error(s):")
        for err in errors:
            print(f"    - {err}")
    print(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
