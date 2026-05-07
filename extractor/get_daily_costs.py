#!/usr/bin/env python3
"""
Fetch Azure costs for the previous and current month.

Output (1 CSV file per run):
  1. daily_costs_by_resource_<customer>_<from>_<to>.csv

All subscriptions for a customer are combined into a single file.
Each row contains the subscription_id and subscription_name so you can
filter in your reporting tool.

Two modes of operation:
    Single subscription:  -s <subscription-id>
    CSV batch:            -i <customer.csv>

Prerequisites:
    pip install azure-identity requests
    az login  (must have an active session — batch mode will re-login per tenant)

Permissions:
    Cost Management Reader (or Reader/Contributor/Owner) on each subscription

Usage:
    python get_daily_costs.py -s <subscription-id>
    python get_daily_costs.py -s <subscription-id> --from 2026-02-01 --to 2026-03-26
    python get_daily_costs.py -i ../customers/CUST.csv
    python get_daily_costs.py -i ../customers/CUST.csv --from 2026-02-01 --to 2026-03-26 --skip-login

CSV output uses European number format (comma as decimal separator, semicolon
delimiter) so the files open correctly in Excel with Belgian/European settings.
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

import requests
from azure.identity import (
    AzureCliCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------
def get_date_range() -> tuple[date, date]:
    """Return (first day of previous month, today)."""
    today = date.today()
    first_of_this_month = today.replace(day=1)
    first_of_last_month = (first_of_this_month - timedelta(days=1)).replace(day=1)
    return first_of_last_month, today


# ---------------------------------------------------------------------------
# Customer CSV reader
# ---------------------------------------------------------------------------
def read_customers(path: str) -> list[dict]:
    """
    Read the customer JSON file and return all subscription rows.
    """
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
            subs.append({
                "tenant_id": tenant_id,
                "subscription_id": sub_id,
                "subscription_name": sub_name,
            })
    return subs


# ---------------------------------------------------------------------------
# Generic Cost Management Query API caller
# ---------------------------------------------------------------------------
def query_cost_api(
    token: str,
    subscription_id: str,
    date_from: date,
    date_to: date,
    granularity: str,
    grouping: list[dict],
) -> tuple[list, list]:
    """
    POST to the Cost Management Query API with pagination and throttle handling.
    Returns (columns, rows).
    """
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/providers/Microsoft.CostManagement/query"
        f"?api-version=2023-03-01&$top=5000"
    )

    payload = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
        },
        "dataset": {
            "granularity": granularity,
            "aggregation": {
                "totalCost": {"name": "Cost", "function": "Sum"},
                "totalCostUSD": {"name": "CostUSD", "function": "Sum"},
            },
            "grouping": grouping,
        },
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    all_rows = []
    columns = []
    next_url = url
    current_payload = payload

    while next_url:
        resp = requests.post(next_url, json=current_payload, headers=headers, timeout=120)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "30"))
            print(f"      Throttled — waiting {retry_after}s ...", flush=True)
            time.sleep(retry_after)
            continue

        resp.raise_for_status()
        data = resp.json()

        props = data.get("properties", {})
        if not columns:
            columns = props.get("columns", [])
        all_rows.extend(props.get("rows", []))

        next_url = props.get("nextLink")
        current_payload = payload

    return columns, all_rows


# ---------------------------------------------------------------------------
# Query 1: Daily costs per resource
# ---------------------------------------------------------------------------
def fetch_daily_costs_by_resource(
    token: str, subscription_id: str, subscription_name: str,
    date_from: date, date_to: date,
) -> list[dict]:
    print("    Fetching daily costs by resource ...", end=" ", flush=True)

    columns, rows = query_cost_api(
        token, subscription_id, date_from, date_to,
        granularity="Daily",
        grouping=[
            {"type": "Dimension", "name": "ResourceId"},
            {"type": "Dimension", "name": "ResourceType"},
        ],
    )

    records = []
    for row in rows:
        resource_id = str(row[3])
        parts = resource_id.split("/")
        rg_name = ""
        for i, part in enumerate(parts):
            if part.lower() == "resourcegroups" and i + 1 < len(parts):
                rg_name = parts[i + 1]
                break
        resource_name = parts[-1] if parts else resource_id

        records.append({
            "subscription_id": subscription_id,
            "subscription_name": subscription_name,
            "date": str(row[2]),
            "resource_id": resource_id,
            "resource_name": resource_name,
            "resource_type": str(row[4]),
            "resource_group": rg_name,
            "cost": float(row[0]),
            "cost_usd": float(row[1]),
            "currency": str(row[5]) if len(row) > 5 else "",
        })

    print(f"{len(records)} rows")
    return records


# ---------------------------------------------------------------------------
# Fetch daily costs for one subscription, return the records
# ---------------------------------------------------------------------------
def fetch_subscription_costs(
    token: str,
    subscription_id: str,
    subscription_name: str,
    date_from: date,
    date_to: date,
) -> list[dict] | None:
    """
    Returns daily cost records or None on error.
    """
    short_id = subscription_id[:8]
    display = subscription_name or subscription_id
    print(f"\n  → {display} ({short_id}...)")

    try:
        return fetch_daily_costs_by_resource(
            token, subscription_id, subscription_name, date_from, date_to)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        print(f"    ERROR: HTTP {status} — skipping this subscription")
        return None
    except Exception as e:
        print(f"    ERROR: {e} — skipping this subscription")
        return None


# ---------------------------------------------------------------------------
# CSV writers (European format)
# ---------------------------------------------------------------------------
def format_eu_number(value) -> str:
    """Format a float with comma as decimal separator for European CSV output."""
    if isinstance(value, float):
        return f"{value:.4f}".replace(".", ",")
    return str(value)


def write_csv(path: Path, records: list[dict], fieldnames: list[str]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        for record in records:
            eu_record = {k: format_eu_number(v) for k, v in record.items()}
            writer.writerow(eu_record)


def write_json(path: Path, payload: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def write_daily_csv(
    out_dir: Path,
    all_daily: list[dict],
    label: str,
    date_from: date,
    date_to: date,
):
    """Write the consolidated daily costs CSV file."""
    suffix = f"{label}_daily_costs_{date_from}_{date_to}"
    p = out_dir / f"{suffix}.csv"
    write_csv(p, all_daily, [
        "subscription_id", "subscription_name",
        "date", "resource_id", "resource_name", "resource_type",
        "resource_group", "cost", "cost_usd", "currency",
    ])
    print(f"\nCSV written: {p}")


def write_daily_json(
    out_dir: Path,
    all_daily: list[dict],
    label: str,
    date_from: date,
    date_to: date,
):
    suffix = f"{label}_daily_costs_{date_from}_{date_to}"
    p = out_dir / f"{suffix}.json"
    write_json(
        p,
        {
            "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "daily_costs_by_resource": all_daily,
        },
    )
    print(f"JSON written: {p}")


# ---------------------------------------------------------------------------
# Console display
# ---------------------------------------------------------------------------
def print_summary(
    all_daily: list[dict],
    date_from: date,
    date_to: date,
    label: str = "",
):
    W = 80
    total     = sum(r["cost"]     for r in all_daily)
    total_usd = sum(r["cost_usd"] for r in all_daily)
    currency  = all_daily[0]["currency"] if all_daily else ""

    print(f"\n{'='*W}")
    if label:
        print(f"  {label}")
    print(f"  Azure Cost Report  |  {date_from} → {date_to}")
    print(f"  Grand total: {total:,.2f} {currency}  ({total_usd:,.2f} USD)")
    print(f"{'='*W}")

    # Daily totals
    daily_totals: dict[str, dict] = {}
    for r in all_daily:
        d = r["date"]
        if d not in daily_totals:
            daily_totals[d] = {"cost": 0.0, "cost_usd": 0.0}
        daily_totals[d]["cost"]     += r["cost"]
        daily_totals[d]["cost_usd"] += r["cost_usd"]

    print(f"\n  Daily Totals (all subscriptions)")
    print(f"  {'Date':<12} {'Cost':>12} {'Cost (USD)':>12}")
    print(f"  {'-'*12} {'-'*12} {'-'*12}")
    for d in sorted(daily_totals.keys()):
        t = daily_totals[d]
        d_fmt = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if (len(d) == 8 and d.isdigit()) else d
        print(f"  {d_fmt:<12} {t['cost']:>12.2f} {t['cost_usd']:>12.2f}")

    print(f"\n{'='*W}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Fetch Azure costs for last month + current month."
    )

    parser.add_argument(
        "-s", "--subscription",
        help="Single subscription ID.",
    )

    parser.add_argument(
        "-i", "--input",
        help="Path to a semicolon-delimited customer CSV (all rows are processed).",
    )
    parser.add_argument(
        "--from", dest="date_from",
        help="Start date (YYYY-MM-DD). Default: first day of previous month.",
    )
    parser.add_argument(
        "--to", dest="date_to",
        help="End date (YYYY-MM-DD). Default: today.",
    )
    parser.add_argument(
        "--output-dir",
        default=".",
        help="Directory for the output files. Default: current directory.",
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
        "--json",
        action="store_true",
        help="Also print raw JSON to stdout.",
    )
    args = parser.parse_args()

    if not args.subscription and not args.input:
        parser.error("Provide either -s/--subscription or -i/--input.")

    # Determine date range — each argument defaults independently
    today = date.today()
    first_of_this_month = today.replace(day=1)
    default_from = (first_of_this_month - timedelta(days=1)).replace(day=1)

    date_from = date.fromisoformat(args.date_from) if args.date_from else default_from
    date_to   = date.fromisoformat(args.date_to)   if args.date_to   else today

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Period: {date_from} → {date_to}")

    # Collector for all daily records
    all_daily: list[dict] = []

    # ----- Single subscription mode -----
    if args.subscription:
        credential = get_credential(
            None,
            args.sp_client_id,
            args.sp_client_secret,
            args.sp_certificate,
        )
        token = get_token(credential)
        sub_id = args.subscription
        result = fetch_subscription_costs(token, sub_id, sub_id, date_from, date_to)
        if result is None:
            sys.exit(1)

        all_daily = result

        if args.output_format in ("csv", "both"):
            write_daily_csv(out_dir, all_daily, sub_id[:8], date_from, date_to)
        if args.output_format in ("json", "both"):
            write_daily_json(out_dir, all_daily, sub_id[:8], date_from, date_to)

        if args.json:
            print(json.dumps({
                "subscription_id": sub_id,
                "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
                "daily_costs_by_resource": all_daily,
            }, indent=2))
        else:
            print_summary(all_daily, date_from, date_to)
        return

    # ----- CSV batch mode -----
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
            print(f"  ⚠ {msg}")
            errors.append(msg)
            continue

        for sub in tenant_subs:
            result = fetch_subscription_costs(
                token,
                sub["subscription_id"],
                sub["subscription_name"],
                date_from,
                date_to,
            )
            if result:
                all_daily.extend(result)
                success += 1
            else:
                errors.append(f"{sub['subscription_name']} ({sub['subscription_id'][:8]}...)")

    # Output
    if args.output_format in ("csv", "both"):
        write_daily_csv(out_dir, all_daily, csv_label, date_from, date_to)
    if args.output_format in ("json", "both"):
        write_daily_json(out_dir, all_daily, csv_label, date_from, date_to)

    if args.json:
        print(json.dumps({
            "input_csv": args.input,
            "period": {"from": date_from.isoformat(), "to": date_to.isoformat()},
            "daily_costs_by_resource": all_daily,
        }, indent=2))
    else:
        print_summary(all_daily, date_from, date_to,
                      f"CSV: {csv_label}  ({success} subscriptions)")

    # Final report
    print(f"\n{'═'*70}")
    print(f"  Done. {success}/{len(subscriptions)} subscriptions processed successfully.")
    if errors:
        print(f"  ⚠ {len(errors)} error(s):")
        for err in errors:
            print(f"    - {err}")
    print(f"{'═'*70}\n")


if __name__ == "__main__":
    main()
