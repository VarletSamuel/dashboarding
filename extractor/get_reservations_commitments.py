#!/usr/bin/env python3
"""
Azure Reservations Commitments Exporter
=======================================
Fetches both Reserved Instances and Savings Plans from Microsoft.Capacity and
exports a unified inventory.

Output:
  - <label>_reservations_commitments_<runDate>_<runDate>.csv
  - <label>_reservations_commitments_<runDate>_<runDate>.json

Examples:
  python get_reservations_commitments.py
  python get_reservations_commitments.py -i ../customers/CUST.json
  python get_reservations_commitments.py -i ../customers/CUST.json --output-dir ./reports/CUST
  python get_reservations_commitments.py --output-format csv
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date
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


CSV_COLUMNS = [
    "tenant_id",
    "commitment_type",
    "order_id",
    "commitment_id",
    "display_name",
    "status",
    "term",
    "purchase_date",
    "expiration_date",
    "benefit_start_date",
    "billing_plan",
    "auto_renew",
    "sku_name",
    "sku_description",
    "reserved_resource_type",
    "region",
    "quantity",
    "applied_scope_type",
    "applied_scopes",
    "billing_scope_id",
    "commitment_currency",
    "commitment_amount",
    "source",
    "run_date",
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


def read_customer_json(json_path: str) -> dict[str, list[tuple[str, str]]]:
    if not os.path.exists(json_path):
        print(f"ERROR: Customer file not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    with open(json_path, encoding="utf-8") as handle:
        data = json.load(handle)

    tenant_map: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for entry in data.get("azure", []):
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


def arm_get_all(token: str, initial_url: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    results: list[dict] = []
    next_url = initial_url

    while next_url:
        response = requests.get(next_url, headers=headers, timeout=120)
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", "30"))
            print(f"      Throttled, retrying in {retry_after}s...", flush=True)
            time.sleep(retry_after)
            continue
        response.raise_for_status()
        payload = response.json()

        if isinstance(payload.get("value"), list):
            results.extend(payload.get("value", []))
        elif isinstance(payload, dict):
            results.append(payload)

        next_url = payload.get("nextLink")

    return results


def _first(data: dict, *keys: str):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return ""


def _fmt_date(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _to_bool_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _to_number(value):
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return str(value)


def _to_csv_value(value):
    if isinstance(value, float):
        rounded = round(value, 6)
        if rounded.is_integer():
            return str(int(rounded))
        return f"{rounded:.6f}".rstrip("0").rstrip(".")
    if value is None:
        return ""
    return str(value)


def _scope_to_string(scope_value) -> str:
    if scope_value is None:
        return ""
    if isinstance(scope_value, list):
        return "|".join(str(x) for x in scope_value if x)
    return str(scope_value)


def parse_reservation_row(tenant_id: str, order: dict, reservation: dict, run_date: str) -> dict:
    op = order.get("properties", {}) if isinstance(order.get("properties"), dict) else {}
    rp = reservation.get("properties", {}) if isinstance(reservation.get("properties"), dict) else {}
    sku = reservation.get("sku", {}) if isinstance(reservation.get("sku"), dict) else {}

    applied_scope_type = _first(rp, "appliedScopeType")
    applied_scopes = _scope_to_string(_first(rp, "appliedScopes", "appliedScopeProperties"))

    return {
        "tenant_id": tenant_id,
        "commitment_type": "ReservedInstance",
        "order_id": order.get("name", ""),
        "commitment_id": reservation.get("name", ""),
        "display_name": _first(rp, "displayName") or reservation.get("name", ""),
        "status": _first(rp, "provisioningState", "status"),
        "term": str(_first(rp, "term")).upper() if _first(rp, "term") else "",
        "purchase_date": _fmt_date(_first(op, "requestDateTime", "purchaseDateTime")),
        "expiration_date": _fmt_date(_first(rp, "expiryDateTime", "expiryDate")),
        "benefit_start_date": _fmt_date(_first(rp, "benefitStartTime", "benefitStartDateTime")),
        "billing_plan": _first(rp, "billingPlan"),
        "auto_renew": _to_bool_str(_first(rp, "renew")),
        "sku_name": _first(sku, "name"),
        "sku_description": _first(rp, "skuDescription"),
        "reserved_resource_type": _first(rp, "reservedResourceType"),
        "region": _first(rp, "location") or _first(reservation, "location"),
        "quantity": _to_number(_first(rp, "quantity")),
        "applied_scope_type": applied_scope_type,
        "applied_scopes": applied_scopes,
        "billing_scope_id": _first(rp, "billingScopeId"),
        "commitment_currency": _first(rp, "purchaseCurrencyCode", "currencyCode"),
        "commitment_amount": _to_number(_first(rp, "effectivePrice", "totalAmount", "price")),
        "source": "Microsoft.Capacity/reservationOrders",
        "run_date": run_date,
    }


def parse_savings_plan_row(tenant_id: str, order: dict, savings_plan: dict, run_date: str) -> dict:
    op = order.get("properties", {}) if isinstance(order.get("properties"), dict) else {}
    sp = savings_plan.get("properties", {}) if isinstance(savings_plan.get("properties"), dict) else {}

    commitment = sp.get("commitment", {}) if isinstance(sp.get("commitment"), dict) else {}
    applied_scopes = _scope_to_string(_first(sp, "appliedScopes", "appliedScopeProperties"))

    return {
        "tenant_id": tenant_id,
        "commitment_type": "SavingsPlan",
        "order_id": order.get("name", ""),
        "commitment_id": savings_plan.get("name", ""),
        "display_name": _first(sp, "displayName") or savings_plan.get("name", ""),
        "status": _first(sp, "provisioningState", "status"),
        "term": str(_first(sp, "term")).upper() if _first(sp, "term") else "",
        "purchase_date": _fmt_date(_first(op, "requestDateTime", "purchaseDateTime")),
        "expiration_date": _fmt_date(_first(sp, "expiryDateTime", "expiryDate")),
        "benefit_start_date": _fmt_date(_first(sp, "benefitStartTime", "benefitStartDateTime")),
        "billing_plan": _first(sp, "billingPlan"),
        "auto_renew": _to_bool_str(_first(sp, "renew")),
        "sku_name": _first(sp, "skuName"),
        "sku_description": _first(sp, "description", "skuDescription"),
        "reserved_resource_type": _first(sp, "resourceType"),
        "region": _first(sp, "location") or _first(savings_plan, "location"),
        "quantity": _to_number(_first(commitment, "amount")),
        "applied_scope_type": _first(sp, "appliedScopeType"),
        "applied_scopes": applied_scopes,
        "billing_scope_id": _first(sp, "billingScopeId"),
        "commitment_currency": _first(commitment, "currencyCode", "grain"),
        "commitment_amount": _to_number(_first(commitment, "amount")),
        "source": "Microsoft.Capacity/savingsPlanOrders",
        "run_date": run_date,
    }


def fetch_reserved_instances(token: str, tenant_id: str, run_date: str) -> list[dict]:
    api_versions = ["2022-11-01", "2021-10-01"]
    orders: list[dict] = []

    for api_version in api_versions:
        url = f"https://management.azure.com/providers/Microsoft.Capacity/reservationOrders?api-version={api_version}"
        try:
            orders = arm_get_all(token, url)
            if orders:
                print(f"  Reserved Instance orders: {len(orders)} (api-version={api_version})")
            break
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            print(f"  RI order list failed on api-version={api_version} (HTTP {status})")

    if not orders:
        return []

    rows: list[dict] = []
    seen_reservation_ids: set[str] = set()

    for order in orders:
        order_id = order.get("name", "")
        if not order_id:
            continue

        reservations = []
        for api_version in api_versions:
            url = (
                "https://management.azure.com/providers/Microsoft.Capacity/"
                f"reservationOrders/{order_id}/reservations?api-version={api_version}"
            )
            try:
                reservations = arm_get_all(token, url)
                break
            except requests.HTTPError:
                continue

        for reservation in reservations:
            reservation_id = reservation.get("name", "")
            if reservation_id and reservation_id in seen_reservation_ids:
                continue
            if reservation_id:
                seen_reservation_ids.add(reservation_id)
            rows.append(parse_reservation_row(tenant_id, order, reservation, run_date))

    return rows


def fetch_savings_plans(token: str, tenant_id: str, run_date: str) -> list[dict]:
    api_versions = ["2022-11-01", "2024-04-01"]
    orders: list[dict] = []

    for api_version in api_versions:
        url = f"https://management.azure.com/providers/Microsoft.Capacity/savingsPlanOrders?api-version={api_version}"
        try:
            orders = arm_get_all(token, url)
            if orders:
                print(f"  Savings Plan orders: {len(orders)} (api-version={api_version})")
            break
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            print(f"  Savings Plan order list failed on api-version={api_version} (HTTP {status})")

    if not orders:
        return []

    rows: list[dict] = []
    seen_plan_ids: set[str] = set()

    for order in orders:
        order_id = order.get("name", "")
        if not order_id:
            continue

        plans = []
        for api_version in api_versions:
            url = (
                "https://management.azure.com/providers/Microsoft.Capacity/"
                f"savingsPlanOrders/{order_id}/savingsPlans?api-version={api_version}"
            )
            try:
                plans = arm_get_all(token, url)
                break
            except requests.HTTPError:
                continue

        for plan in plans:
            plan_id = plan.get("name", "")
            if plan_id and plan_id in seen_plan_ids:
                continue
            if plan_id:
                seen_plan_ids.add(plan_id)
            rows.append(parse_savings_plan_row(tenant_id, order, plan, run_date))

    return rows


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, delimiter=";")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _to_csv_value(row.get(k, "")) for k in CSV_COLUMNS})
    print(f"  Wrote CSV: {path}")


def write_json(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2)
    print(f"  Wrote JSON: {path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export Azure Reserved Instances and Savings Plans to CSV/JSON.",
    )
    parser.add_argument(
        "-i",
        "--input",
        metavar="PATH",
        help="Path to customer JSON file (expects .azure[] with tenant/subscription entries).",
    )
    parser.add_argument(
        "--tenant-id",
        default=os.environ.get("AZURE_TENANT_ID"),
        help="Single tenant mode when --input is not used.",
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
        help="Output format: csv, json or both (default: both).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also print JSON payload to stdout.",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    run_date = date.today().isoformat()
    out_dir = Path(args.output_dir)

    all_rows: list[dict] = []

    if args.input:
        tenant_map = read_customer_json(args.input)
        label = Path(args.input).stem.upper()

        for tenant_id in tenant_map.keys():
            print(f"\nCollecting commitments for tenant: {tenant_id}")
            try:
                credential = get_credential(
                    tenant_id=tenant_id,
                    sp_client_id=args.sp_client_id,
                    sp_client_secret=args.sp_client_secret,
                    sp_certificate=args.sp_certificate,
                )
                token = get_token(credential)
            except Exception as exc:
                print(f"  Skipping tenant due to auth error: {exc}")
                continue

            try:
                ri_rows = fetch_reserved_instances(token, tenant_id, run_date)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                print(f"  Reserved Instances query failed (HTTP {status})")
                ri_rows = []

            try:
                sp_rows = fetch_savings_plans(token, tenant_id, run_date)
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "?"
                print(f"  Savings Plans query failed (HTTP {status})")
                sp_rows = []

            print(f"  Tenant result: {len(ri_rows)} RI + {len(sp_rows)} Savings Plans")
            all_rows.extend(ri_rows)
            all_rows.extend(sp_rows)
    else:
        label = "export"
        print("Collecting commitments for current login context...")
        credential = get_credential(
            tenant_id=args.tenant_id,
            sp_client_id=args.sp_client_id,
            sp_client_secret=args.sp_client_secret,
            sp_certificate=args.sp_certificate,
        )
        token = get_token(credential)
        tenant_label = args.tenant_id or "current"

        ri_rows = fetch_reserved_instances(token, tenant_label, run_date)
        sp_rows = fetch_savings_plans(token, tenant_label, run_date)
        all_rows.extend(ri_rows)
        all_rows.extend(sp_rows)

    if not all_rows:
        print("No reservations commitments found.")
        sys.exit(0)

    # Deduplicate by (type, commitment_id), useful when tenant contexts overlap.
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for row in all_rows:
        key = (str(row.get("commitment_type", "")), str(row.get("commitment_id", "")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    base = f"{label}_reservations_commitments_{run_date}_{run_date}"
    csv_path = out_dir / f"{base}.csv"
    json_path = out_dir / f"{base}.json"

    if args.output_format in ("csv", "both"):
        write_csv(csv_path, deduped)
    if args.output_format in ("json", "both"):
        write_json(json_path, deduped)

    if args.json:
        print(json.dumps(deduped, indent=2))

    print(f"\nTotal commitments exported: {len(deduped)}")


if __name__ == "__main__":
    main()
