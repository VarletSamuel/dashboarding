#!/usr/bin/env python3
"""
Audit Entra ID app registration client secret expiry (and optionally certs).

This extractor is wrapper-compatible:
- accepts customer input JSON (-i) with tenant IDs
- supports service-principal authentication flags
- writes reports to --output-dir in CSV/JSON format

Permissions (Microsoft Graph): Application.Read.All
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone


_REQUIRED = {
    "azure.identity": "azure-identity",
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
        print(f"    x  {pkg}")
    print(f"\nPython executable : {sys.executable}")
    print(f"Python version    : {sys.version}")
    print("\nInstall into the same Python that runs this script:")
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


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"

SUMMARY_COLUMNS = [
    "tenant_id",
    "credential_type",
    "status",
    "days_remaining",
    "end_utc",
    "start_utc",
    "app_display_name",
    "app_id",
    "object_id",
    "credential_display_name",
    "key_id",
]


def read_customer_json(json_path: str) -> dict:
    if not os.path.exists(json_path):
        print(f"ERROR: Customer file not found: {json_path}")
        sys.exit(1)
    with open(json_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_tenants_from_customer_json(json_path: str) -> list[str]:
    data = read_customer_json(json_path)
    tenant_map = defaultdict(list)
    for entry in data.get("azure", []):
        status = str(entry.get("status") or "").strip().lower()
        if status != "active":
            continue
        tenant = (entry.get("tenant_id") or "").strip()
        sub_id = (entry.get("subscription_id") or "").strip()
        if tenant and sub_id:
            tenant_map[tenant].append(sub_id)

    if not tenant_map:
        print(f"ERROR: No tenant entries found in {json_path}")
        sys.exit(1)

    return list(tenant_map.keys())


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

    chain_candidates.append(DefaultAzureCredential(additionally_allowed_tenants=["*"]))
    if tenant_id:
        chain_candidates.append(AzureCliCredential(tenant_id=tenant_id))
    else:
        chain_candidates.append(AzureCliCredential())

    return ChainedTokenCredential(*chain_candidates)


def graph_get_token(credential) -> str:
    return credential.get_token(GRAPH_SCOPE).token


def iter_applications(token: str):
    """Yield every application object in the tenant, following @odata.nextLink."""
    url = (
        f"{GRAPH_BASE}/applications"
        "?$select=id,appId,displayName,passwordCredentials,keyCredentials"
        "&$top=999"
    )
    headers = {"Authorization": f"Bearer {token}"}
    while url:
        response = requests.get(url, headers=headers, timeout=120)
        response.raise_for_status()
        payload = response.json()
        for app in payload.get("value", []):
            yield app
        url = payload.get("@odata.nextLink")


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def classify(end_dt: datetime, warn_days: int, now: datetime) -> tuple[int, str]:
    days = (end_dt - now).days
    if end_dt < now:
        return days, "expired"
    if days <= warn_days:
        return days, "expiring"
    return days, "valid"


def collect_for_tenant(token: str, tenant_id: str, warn_days: int, include_certs: bool) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows: list[dict] = []

    for app in iter_applications(token):
        base = {
            "tenant_id": tenant_id,
            "app_display_name": app.get("displayName", "") or "",
            "app_id": app.get("appId", "") or "",
            "object_id": app.get("id", "") or "",
        }

        for cred in app.get("passwordCredentials") or []:
            end_dt = parse_dt(cred["endDateTime"])
            days, status = classify(end_dt, warn_days, now)
            rows.append(
                {
                    **base,
                    "credential_type": "secret",
                    "status": status,
                    "days_remaining": days,
                    "end_utc": cred.get("endDateTime", ""),
                    "start_utc": cred.get("startDateTime", ""),
                    "credential_display_name": cred.get("displayName") or "",
                    "key_id": cred.get("keyId", ""),
                }
            )

        if include_certs:
            for cred in app.get("keyCredentials") or []:
                end_dt = parse_dt(cred["endDateTime"])
                days, status = classify(end_dt, warn_days, now)
                rows.append(
                    {
                        **base,
                        "credential_type": "certificate",
                        "status": status,
                        "days_remaining": days,
                        "end_utc": cred.get("endDateTime", ""),
                        "start_utc": cred.get("startDateTime", ""),
                        "credential_display_name": cred.get("displayName") or "",
                        "key_id": cred.get("keyId", ""),
                    }
                )

    return rows


def filter_rows(rows: list[dict], only: str | None) -> list[dict]:
    if not only:
        return rows
    wanted = set(only.split("+"))
    return [row for row in rows if row.get("status") in wanted]


def print_table(rows: list[dict]) -> None:
    order = {"expired": 0, "expiring": 1, "valid": 2}
    rows = sorted(rows, key=lambda row: (order.get(row["status"], 99), int(row["days_remaining"])))

    header = (
        f"{'Tenant':<36} {'Status':<9} {'Days':>5}  {'Type':<11} "
        f"{'App':<45} {'End (UTC)':<20} {'Cred name'}"
    )
    print(header)
    print("-" * len(header))

    for row in rows:
        line = (
            f"{row.get('tenant_id', ''):<36} {row.get('status', ''):<9} "
            f"{str(row.get('days_remaining', '')):>5}  "
            f"{row.get('credential_type', ''):<11} "
            f"{str(row.get('app_display_name', ''))[:45]:<45} "
            f"{str(row.get('end_utc', ''))[:19]:<20} "
            f"{row.get('credential_display_name', '')}"
        )
        print(line)


def write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS, delimiter=";")
        writer.writeheader()
        writer.writerows([{col: row.get(col, "") for col in SUMMARY_COLUMNS} for row in rows])


def write_json(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2)


def build_output_paths(args, customer_prefix: str | None) -> tuple[str, str]:
    os.makedirs(args.output_dir, exist_ok=True)
    today = datetime.now(timezone.utc).date().isoformat()

    if customer_prefix:
        csv_name = f"{customer_prefix}_app_secret_expirations_summary_{today}.csv"
        json_name = f"{customer_prefix}_app_secret_expirations_summary_{today}.json"
    else:
        csv_name = f"app_secret_expirations_summary_{today}.csv"
        json_name = f"app_secret_expirations_summary_{today}.json"

    return os.path.join(args.output_dir, csv_name), os.path.join(args.output_dir, json_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Entra app secret/certificate expiry.")
    parser.add_argument("-i", "--input", default=None, help="Customer JSON path containing tenant/subscription list.")
    parser.add_argument("--tenant-id", default=None, help="Single tenant ID (alternative to -i).")
    parser.add_argument("--skip-login", action="store_true", help="Deprecated no-op; kept for wrapper compatibility.")
    parser.add_argument("--warn-days", type=int, default=30, help="Expiring threshold in days (default: 30).")
    parser.add_argument("--include-certs", action="store_true", help="Include keyCredentials (certificates).")
    parser.add_argument("--only", choices=["expired", "expiring", "expired+expiring", "valid"], help="Filter rows by status bucket(s).")
    parser.add_argument("--fail-on", choices=["expired", "expiring"], help="Exit non-zero if any row matches threshold condition.")
    parser.add_argument("--sp-client-id", default=None, metavar="APP_ID", help="App Registration client ID for service principal auth.")
    parser.add_argument(
        "--sp-client-secret",
        default=os.environ.get("AZURE_SP_CLIENT_SECRET"),
        metavar="SECRET",
        help="Client secret for service principal auth (fallback: AZURE_SP_CLIENT_SECRET).",
    )
    parser.add_argument("--sp-certificate", default=None, metavar="CERT_PATH", help="Path to PEM certificate for service principal auth.")
    parser.add_argument("--output-format", choices=("csv", "json", "both"), default="csv", help="Output format (default: csv).")
    parser.add_argument("--output-dir", default=".", help="Output directory (default: current directory).")
    args = parser.parse_args()

    if not args.input and not args.tenant_id:
        print("ERROR: Provide -i/--input or --tenant-id", file=sys.stderr)
        return 1

    tenants: list[str]
    customer_prefix = None
    if args.input:
        tenants = read_tenants_from_customer_json(args.input)
        customer_prefix = os.path.splitext(os.path.basename(args.input))[0].upper()
    else:
        tenants = [args.tenant_id]

    all_rows: list[dict] = []
    failures: list[str] = []

    for tenant_id in tenants:
        print(f"\n-- Tenant: {tenant_id}")
        try:
            credential = get_credential(
                tenant_id=tenant_id,
                sp_client_id=args.sp_client_id,
                sp_client_secret=args.sp_client_secret,
                sp_certificate=args.sp_certificate,
            )
            token = graph_get_token(credential)
            rows = collect_for_tenant(token, tenant_id, args.warn_days, args.include_certs)
            all_rows.extend(rows)
            print(f"   Collected {len(rows)} credential row(s)")
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            failures.append(f"{tenant_id}: HTTP {status}")
            print(f"   ERROR: Graph call failed for tenant {tenant_id}: {exc}")
        except Exception as exc:
            failures.append(f"{tenant_id}: {exc}")
            print(f"   ERROR: Failed for tenant {tenant_id}: {exc}")

    rows = filter_rows(all_rows, args.only)
    print_table(rows)

    expired = sum(1 for row in rows if row.get("status") == "expired")
    expiring = sum(1 for row in rows if row.get("status") == "expiring")
    valid = sum(1 for row in rows if row.get("status") == "valid")
    print(
        f"\nTotal: {len(rows)}  |  expired: {expired}  "
        f"|  expiring (<={args.warn_days}d): {expiring}  |  valid: {valid}"
    )

    csv_path, json_path = build_output_paths(args, customer_prefix)

    if args.output_format in ("csv", "both"):
        write_csv(csv_path, rows)
        print(f"CSV written: {csv_path}")
    if args.output_format in ("json", "both"):
        write_json(json_path, rows)
        print(f"JSON written: {json_path}")

    if failures:
        print("\nSome tenants failed:")
        for item in failures:
            print(f"  - {item}")

    if args.fail_on == "expired" and expired:
        return 2
    if args.fail_on == "expiring" and (expired or expiring):
        return 2
    return 1 if failures and not rows else 0


if __name__ == "__main__":
    sys.exit(main())
