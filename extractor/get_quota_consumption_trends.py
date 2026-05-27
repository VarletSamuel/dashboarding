#!/usr/bin/env python3
"""
Azure Quota Consumption Trends Exporter
=======================================
Collects Azure Compute regional quota usage snapshots and builds:
  1) summary file (latest point per quota key)
  2) timeseries file (historical points merged from prior snapshot files)

Output files:
  - <label>_quota_consumption_trends_summary_<from>_<to>.csv
  - <label>_quota_consumption_trends_timeseries_<from>_<to>.csv
  - Optional JSON equivalents when --output-format json|both is used

Notes:
- Azure does not expose historical quota-usage time series directly via ARM.
  This extractor creates trends by combining today's snapshot with prior
  snapshot CSVs already present in the output folder.
- Quota source currently targets Microsoft.Compute regional usages.

Examples:
  python get_quota_consumption_trends.py -i ../customers/CUST.json --output-dir ./reports/CUST
  python get_quota_consumption_trends.py -i ../customers/CUST.json --from 2026-01-01 --to 2026-04-30
  python get_quota_consumption_trends.py -i ../customers/CUST.json --locations westeurope,northeurope
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

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

from azure.identity import (  # noqa: E402
    AzureCliCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
)

SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "location",
    "quota_name",
    "quota_code",
    "unit",
    "current_value",
    "limit",
    "remaining",
    "utilization_pct",
    "source",
    "run_date",
]

TIMESERIES_COLUMNS = list(SUMMARY_COLUMNS)

LOCATION_API_VERSION = "2022-12-01"
COMPUTE_USAGE_API_VERSIONS = ["2023-07-01", "2022-03-01"]


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


def read_customer_json(json_path: str) -> dict[str, list[tuple[str, str]]]:
    if not os.path.exists(json_path):
        print(f"ERROR: Customer file not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    with open(json_path, encoding="utf-8") as handle:
        data = json.load(handle)

    tenant_map: dict[str, list[tuple[str, str]]] = defaultdict(list)
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
        print(f"ERROR: No subscriptions found in {json_path}", file=sys.stderr)
        sys.exit(1)

    total_subs = sum(len(v) for v in tenant_map.values())
    print(f"Input '{json_path}': {total_subs} subscription(s) across {len(tenant_map)} tenant(s)")
    return dict(tenant_map)


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def get_default_range() -> tuple[date, date]:
    today = date.today()
    first_of_this_month = today.replace(day=1)
    first_of_last_month = (first_of_this_month - timedelta(days=1)).replace(day=1)
    return first_of_last_month, today


def parse_effective_range(date_from: str | None, date_to: str | None) -> tuple[date, date]:
    default_from, default_to = get_default_range()
    effective_from = parse_date(date_from) if date_from else default_from
    effective_to = parse_date(date_to) if date_to else default_to
    if effective_from > effective_to:
        raise ValueError(f"--from ({effective_from}) must be <= --to ({effective_to})")
    return effective_from, effective_to


def arm_get(token: str, url: str) -> dict:
    headers = {"Authorization": f"Bearer {token}"}
    while True:
        response = requests.get(url, headers=headers, timeout=120)
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "20"))
            print(f"      Throttled, retrying in {retry_after}s...", flush=True)
            time.sleep(retry_after)
            continue
        response.raise_for_status()
        return response.json()


def arm_get_all(token: str, initial_url: str) -> list[dict]:
    items: list[dict] = []
    next_url = initial_url
    while next_url:
        payload = arm_get(token, next_url)
        values = payload.get("value")
        if isinstance(values, list):
            items.extend(values)
        next_url = payload.get("nextLink")
    return items


def list_subscription_locations(token: str, subscription_id: str, include_locations: set[str] | None) -> list[str]:
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}/locations"
        f"?api-version={LOCATION_API_VERSION}"
    )
    try:
        locations_payload = arm_get(token, url)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        print(f"    Could not list locations for {subscription_id} (HTTP {status}); skipping subscription")
        return []

    raw_locations = locations_payload.get("value", [])
    results: list[str] = []

    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        if not name:
            continue

        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        region_type = str(metadata.get("regionType") or "").strip().lower()
        if region_type and region_type != "physical":
            continue

        if include_locations is not None and name not in include_locations:
            continue

        results.append(name)

    return sorted(set(results))


def fetch_compute_location_usages(token: str, subscription_id: str, location: str) -> list[dict]:
    last_error: requests.HTTPError | None = None

    for api_version in COMPUTE_USAGE_API_VERSIONS:
        url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/providers/Microsoft.Compute/locations/{location}/usages"
            f"?api-version={api_version}"
        )
        try:
            return arm_get_all(token, url)
        except requests.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else "?"
            if status in (400, 404):
                continue
            raise

    if last_error is not None:
        status = last_error.response.status_code if last_error.response is not None else "?"
        print(f"      Compute usage unavailable for {location} (HTTP {status})")
    return []


def parse_number(value):
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        return value
    try:
        if "." in str(value):
            return float(value)
        return int(value)
    except (TypeError, ValueError):
        return 0


def to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def safe_pct(current_value, limit_value) -> float:
    limit = to_float(limit_value)
    if limit <= 0:
        return 0.0
    return round((to_float(current_value) / limit) * 100.0, 4)


def normalize_usage_row(
    tenant_id: str,
    subscription_id: str,
    subscription_name: str,
    location: str,
    usage_item: dict,
    run_date: str,
) -> dict:
    name_obj = usage_item.get("name") if isinstance(usage_item.get("name"), dict) else {}

    quota_code = str(name_obj.get("value") or usage_item.get("name") or "")
    quota_name = str(name_obj.get("localizedValue") or quota_code)
    unit = str(usage_item.get("unit") or "")

    current_value = parse_number(usage_item.get("currentValue"))
    limit_value = parse_number(usage_item.get("limit"))
    remaining = to_float(limit_value) - to_float(current_value)

    return {
        "tenant_id": tenant_id,
        "subscription_id": subscription_id,
        "subscription_name": subscription_name,
        "location": location,
        "quota_name": quota_name,
        "quota_code": quota_code,
        "unit": unit,
        "current_value": current_value,
        "limit": limit_value,
        "remaining": round(remaining, 4),
        "utilization_pct": safe_pct(current_value, limit_value),
        "source": "Microsoft.Compute/locations/usages",
        "run_date": run_date,
    }


def row_sort_key(row: dict) -> tuple:
    return (
        row.get("subscription_name", ""),
        row.get("subscription_id", ""),
        row.get("location", ""),
        row.get("quota_code", ""),
        row.get("run_date", ""),
    )


def historical_pattern(prefix: str) -> re.Pattern[str]:
    escaped_prefix = re.escape(prefix)
    return re.compile(
        rf"^{escaped_prefix}_quota_consumption_trends_summary_(\d{{4}}-\d{{2}}-\d{{2}})_(\d{{4}}-\d{{2}}-\d{{2}})\.csv$"
    )


def load_historical_summary_rows(output_dir: Path, prefix: str, date_from: date, date_to: date) -> list[dict]:
    pattern = historical_pattern(prefix)
    rows: list[dict] = []

    for file_path in output_dir.glob("*.csv"):
        match = pattern.match(file_path.name)
        if not match:
            continue

        file_from = parse_date(match.group(1))
        file_to = parse_date(match.group(2))
        if file_to < date_from or file_from > date_to:
            continue

        try:
            with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle, delimiter=";")
                for row in reader:
                    run_date_str = (row.get("run_date") or "").strip()
                    try:
                        run_day = parse_date(run_date_str)
                    except ValueError:
                        continue
                    if date_from <= run_day <= date_to:
                        rows.append({k: row.get(k, "") for k in SUMMARY_COLUMNS})
        except OSError as exc:
            print(f"  Warning: could not read historical file {file_path.name}: {exc}")

    return rows


def dedupe_timeseries_rows(rows: list[dict]) -> list[dict]:
    by_key: dict[tuple, dict] = {}
    for row in rows:
        key = (
            row.get("tenant_id", ""),
            row.get("subscription_id", ""),
            row.get("location", ""),
            row.get("quota_code", ""),
            row.get("run_date", ""),
        )
        by_key[key] = row
    return sorted(by_key.values(), key=row_sort_key)


def latest_summary_from_timeseries(rows: list[dict]) -> list[dict]:
    latest: dict[tuple, dict] = {}
    for row in rows:
        key = (
            row.get("tenant_id", ""),
            row.get("subscription_id", ""),
            row.get("location", ""),
            row.get("quota_code", ""),
        )
        candidate_date = str(row.get("run_date") or "")
        existing = latest.get(key)
        if existing is None or candidate_date > str(existing.get("run_date") or ""):
            latest[key] = row

    return sorted(latest.values(), key=row_sort_key)


def write_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in columns})


def write_json(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2)


def parse_locations(raw_locations: str | None) -> set[str] | None:
    if not raw_locations:
        return None
    parsed = {part.strip().lower() for part in raw_locations.split(",") if part.strip()}
    return parsed or None


def collect_current_snapshot_rows(args: argparse.Namespace, run_date: str) -> tuple[list[dict], str]:
    rows: list[dict] = []

    include_locations = parse_locations(args.locations)

    if args.input:
        tenant_map = read_customer_json(args.input)
        label = Path(args.input).stem.upper()

        for tenant_id, subscriptions in tenant_map.items():
            print(f"\nTenant: {tenant_id}")
            try:
                credential = get_credential(
                    tenant_id=tenant_id,
                    sp_client_id=args.sp_client_id,
                    sp_client_secret=args.sp_client_secret,
                    sp_certificate=args.sp_certificate,
                )
                token = get_token(credential)
            except Exception as exc:
                print(f"  Skipping tenant (auth error): {exc}")
                continue

            for subscription_id, subscription_name in subscriptions:
                print(f"  Subscription: {subscription_name or subscription_id}")
                locations = list_subscription_locations(token, subscription_id, include_locations)
                if not locations:
                    print("    No eligible regions found")
                    continue

                print(f"    Regions: {len(locations)}")
                for location in locations:
                    try:
                        usage_items = fetch_compute_location_usages(token, subscription_id, location)
                    except requests.HTTPError as exc:
                        status = exc.response.status_code if exc.response is not None else "?"
                        print(f"      {location}: failed (HTTP {status})")
                        continue

                    if not usage_items:
                        continue

                    for item in usage_items:
                        rows.append(
                            normalize_usage_row(
                                tenant_id,
                                subscription_id,
                                subscription_name,
                                location,
                                item,
                                run_date,
                            )
                        )

    elif args.subscription:
        label = "export"
        tenant_label = args.tenant_id or "current"
        credential = get_credential(
            tenant_id=args.tenant_id,
            sp_client_id=args.sp_client_id,
            sp_client_secret=args.sp_client_secret,
            sp_certificate=args.sp_certificate,
        )
        token = get_token(credential)

        locations = list_subscription_locations(token, args.subscription, include_locations)
        for location in locations:
            usage_items = fetch_compute_location_usages(token, args.subscription, location)
            for item in usage_items:
                rows.append(
                    normalize_usage_row(
                        tenant_label,
                        args.subscription,
                        args.subscription,
                        location,
                        item,
                        run_date,
                    )
                )
    else:
        raise ValueError("Either --input or --subscription must be provided.")

    return rows, label


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export Azure quota usage snapshots and build quota consumption trends.",
    )
    parser.add_argument(
        "-i",
        "--input",
        metavar="PATH",
        help="Path to customer JSON file (expects .azure[] with tenant/subscription entries).",
    )
    parser.add_argument(
        "-s",
        "--subscription",
        help="Single subscription mode (when --input is not used).",
    )
    parser.add_argument(
        "--tenant-id",
        default=os.environ.get("AZURE_TENANT_ID"),
        help="Single-tenant mode value used with --subscription.",
    )
    parser.add_argument(
        "--locations",
        default=None,
        help="Optional comma-separated Azure regions (for example: westeurope,northeurope).",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Trend window start date YYYY-MM-DD (default: first day of previous month).",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="Trend window end date YYYY-MM-DD (default: today).",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Compatibility no-op; accepted for wrapper parity with other extractors.",
    )
    parser.add_argument(
        "--sp-client-id",
        default=None,
        metavar="APP_ID",
        help="App Registration client ID for service principal auth.",
    )
    parser.add_argument(
        "--sp-client-secret",
        default=os.environ.get("AZURE_SP_CLIENT_SECRET"),
        metavar="SECRET",
        help="Client secret for service principal auth. Falls back to AZURE_SP_CLIENT_SECRET.",
    )
    parser.add_argument(
        "--sp-certificate",
        default=None,
        metavar="CERT_PATH",
        help="Path to PEM certificate for service principal auth.",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        default="./reports",
        help="Directory for output files (default: ./reports).",
    )
    parser.add_argument(
        "--output-format",
        choices=("csv", "json", "both"),
        default="both",
        help="Output format: csv, json, or both (default: both).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also print summary JSON payload to stdout.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    if bool(args.input) == bool(args.subscription):
        parser.error("Provide either --input or --subscription (exactly one).")

    if args.sp_certificate and args.sp_client_secret:
        parser.error("Use either --sp-certificate or --sp-client-secret, not both.")

    trend_from, trend_to = parse_effective_range(args.date_from, args.date_to)
    print(f"Trend window: {trend_from} -> {trend_to}")

    run_date = date.today().isoformat()
    out_dir = Path(args.output_dir)

    current_rows, label = collect_current_snapshot_rows(args, run_date)
    if not current_rows:
        print("No quota usage rows returned from Azure.")
        sys.exit(0)

    historical_rows = load_historical_summary_rows(out_dir, label, trend_from, trend_to)
    all_timeseries_rows = dedupe_timeseries_rows(historical_rows + current_rows)

    # Keep only points inside the requested trend window.
    filtered_timeseries: list[dict] = []
    for row in all_timeseries_rows:
        try:
            run_day = parse_date(str(row.get("run_date") or ""))
        except ValueError:
            continue
        if trend_from <= run_day <= trend_to:
            filtered_timeseries.append(row)

    latest_summary_rows = latest_summary_from_timeseries(filtered_timeseries)

    range_suffix = f"{trend_from.isoformat()}_{trend_to.isoformat()}"
    summary_name = f"{label}_quota_consumption_trends_summary_{range_suffix}.csv"
    timeseries_name = f"{label}_quota_consumption_trends_timeseries_{range_suffix}.csv"

    summary_path = out_dir / summary_name
    timeseries_path = out_dir / timeseries_name
    summary_json_path = out_dir / summary_name.replace(".csv", ".json")
    timeseries_json_path = out_dir / timeseries_name.replace(".csv", ".json")

    if args.output_format in ("csv", "both"):
        write_csv(summary_path, latest_summary_rows, SUMMARY_COLUMNS)
        write_csv(timeseries_path, filtered_timeseries, TIMESERIES_COLUMNS)

    if args.output_format in ("json", "both"):
        write_json(summary_json_path, latest_summary_rows)
        write_json(timeseries_json_path, filtered_timeseries)

    print(f"\nSummary rows: {len(latest_summary_rows)}")
    print(f"Timeseries rows: {len(filtered_timeseries)}")

    if args.output_format in ("csv", "both"):
        print(f"  -> {summary_path}")
        print(f"  -> {timeseries_path}")
    if args.output_format in ("json", "both"):
        print(f"  -> {summary_json_path}")
        print(f"  -> {timeseries_json_path}")

    if args.json:
        print(json.dumps(latest_summary_rows, indent=2))


if __name__ == "__main__":
    main()
