#!/usr/bin/env python3
"""
Azure Reserved Instances Exporter
==================================
Fetches all reservation orders and their individual reservations (including
1-day, 7-day and 30-day utilisation) from the Azure Management API and writes
them to a CSV file compatible with the RI Advisor dashboard.

Output: comma-delimited CSV (matches the format from the Azure portal export).

Prerequisites
-------------
    pip install azure-identity azure-mgmt-reservations azure-mgmt-consumption

Authentication
--------------
    Relies on an active `az login` session (AzureCliCredential).
    For multi-tenant / multi-customer use, pass -c / -i to automatically
    re-login per tenant, the same pattern as get_daily_costs.py.

Permissions
-----------
    Reader role on the reservation orders is enough.
    Consumption API (utilisation) requires the billing-scope Reader or
    "Reservations Reader" role.

Usage
-----
    # All reservations visible to the current az-login session:
    python get_reserved_instances.py

    # Single tenant, skip re-login:
    python get_reserved_instances.py --skip-login

    # Customer batch (reads customers.csv):
    python get_reserved_instances.py -c CUST -i bin/customers.csv
    python get_reserved_instances.py -c CUST -i bin/customers.csv --skip-login

    # Custom output directory:
    python get_reserved_instances.py -c CUST -i bin/customers.csv --output-dir ./reports/CUST

Notes
-----
- The Reservations API is a *tenant*-level (billing) API, not a
  subscription-level API. The script therefore de-duplicates by
  reservation ID — running against multiple subscriptions in the same
  tenant will NOT produce duplicate rows.
- Utilisation is fetched from the Consumption "ReservationSummaries" API
  using the "daily" grain. The most-recent 1-day, 7-day average and 30-day
  average are derived from the last N daily rows.
- Reservations whose utilisation cannot be fetched (e.g. no permission on
  the Consumption API) are still exported; utilisation columns are left blank.
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# ── Dependency check ──────────────────────────────────────────────────────────
_REQUIRED = {
    "azure.identity":          "azure-identity",
    "azure.mgmt.reservations": "azure-mgmt-reservations",
    "azure.mgmt.consumption":  "azure-mgmt-consumption",
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

from azure.core.exceptions import HttpResponseError
from azure.identity import (
    AzureCliCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
)
from azure.mgmt.consumption import ConsumptionManagementClient
from azure.mgmt.reservations import AzureReservationAPI

# ── CSV columns (matches Azure portal reservedInstances export) ───────────────
CSV_COLUMNS = [
    "Name",
    "Reservation Id",
    "Reservation order Id",
    "Status",
    "Expiration date",
    "Purchase date",
    "Term",
    "Scope",
    "Scope subscription",
    "Scope resource group",
    "Type",
    "Product name",
    "Region",
    "Quantity",
    "Utilization % 1 Day",
    "Utilization % 7 Day",
    "Utilization % 30 Day",
    "Deep link to reservation",
]

# Placeholder subscription_id required by ConsumptionManagementClient constructor
# but not used in reservation-summary URL paths (which are tenant-scoped).
_PLACEHOLDER_SUB = "00000000-0000-0000-0000-000000000000"

PORTAL_RI_LINK = (
    "https://portal.azure.com#resource/providers/microsoft.capacity"
    "/reservationOrders/{order_id}/reservations/{reservation_id}/overview"
)


# ── Auth ──────────────────────────────────────────────────────────────────────

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


# ── Customer CSV reader ───────────────────────────────────────────────────────

def read_customer_csv(json_path: str) -> dict[str, list[tuple[str, str]]]:
    """
    Read the customer JSON file.
    Returns { tenant_id: [(subscription_id, subscription_name), ...] }
    Only one tenant entry per unique tenant_id is needed (reservations API is
    tenant-scoped), but we still build the map so we can re-login per tenant.
    """
    if not os.path.exists(json_path):
        print(f"ERROR: Customer file not found: {json_path}", file=sys.stderr)
        sys.exit(1)

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

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


# ── SDK clients ──────────────────────────────────────────────────────────────

def _make_reservations_client(credential) -> AzureReservationAPI:
    return AzureReservationAPI(credential=credential)


def _make_consumption_client(credential) -> ConsumptionManagementClient:
    # subscription_id is required by the constructor but is not included in
    # the reservation-summary URL path, which is tenant-scoped.
    return ConsumptionManagementClient(
        credential=credential,
        subscription_id=_PLACEHOLDER_SUB,
    )


# ── Reservation orders & reservations ─────────────────────────────────────────

def list_reservation_orders(res_client: AzureReservationAPI) -> list:
    """List all reservation orders accessible to the current credential."""
    orders = list(res_client.reservation_order.list())
    print(f"  Found {len(orders)} reservation order(s)")
    return orders


def list_reservations_in_order(res_client: AzureReservationAPI, order_id: str) -> list:
    """List all reservations inside a single order."""
    return list(res_client.reservation.list(reservation_order_id=order_id))


# ── Utilisation ───────────────────────────────────────────────────────────────

def fetch_utilisation(cons_client: ConsumptionManagementClient, order_id: str, reservation_id: str) -> dict:
    """
    Fetch daily-grain reservation summaries for the past 30 days and return
    {util_1d, util_7d, util_30d} as floats 0-100 or None if unavailable.
    """
    today = date.today()
    start = (today - timedelta(days=30)).isoformat()
    end = today.isoformat()
    filter_expr = f"properties/usageDate ge '{start}' AND properties/usageDate le '{end}'"

    try:
        summaries = list(
            cons_client.reservations_summaries.list_by_reservation_order_and_reservation(
                reservation_order_id=order_id,
                reservation_id=reservation_id,
                grain="daily",
                filter=filter_expr,
            )
        )
    except HttpResponseError as e:
        print(f"\n    ⚠  Utilisation HTTP error ({order_id}/{reservation_id}): {e.status_code} {e.reason}")
        return {"util_1d": None, "util_7d": None, "util_30d": None}
    except Exception as e:
        print(f"\n    ⚠  Utilisation error ({order_id}/{reservation_id}): {type(e).__name__}: {e}")
        return {"util_1d": None, "util_7d": None, "util_30d": None}

    if not summaries:
        return {"util_1d": None, "util_7d": None, "util_30d": None}

    def _date_key(s):
        ud = getattr(s.properties, "usage_date", None)
        if ud is None:
            return ""
        return ud.isoformat() if hasattr(ud, "isoformat") else str(ud)

    summaries.sort(key=_date_key)

    def _avg_util(rows):
        vals = [
            v for v in (
                getattr(r.properties, "avg_utilization_percentage", None)
                for r in rows
            )
            if v is not None
        ]
        return round(sum(vals) / len(vals), 2) if vals else None

    return {
        "util_1d":  _avg_util(summaries[-1:]),
        "util_7d":  _avg_util(summaries[-7:]),
        "util_30d": _avg_util(summaries[-30:]),
    }


# ── Normalise a reservation to a CSV row ──────────────────────────────────────

def _fmt_date(dt) -> str:
    """Format a datetime/date object or ISO string to a plain string."""
    if dt is None:
        return ""
    return dt.isoformat() if hasattr(dt, "isoformat") else str(dt)


def reservation_to_row(order, reservation, utilisation: dict) -> dict:
    """Map SDK model objects to the CSV column dict.

    Handles both track-1 (msrest, v2.x — properties flattened on the object)
    and track-2 (azure-mgmt-core, v3+) where they live under .properties.
    """
    oprops = getattr(order, "properties", order)
    rprops = getattr(reservation, "properties", reservation)

    order_id       = order.name or ""
    reservation_id = reservation.name or ""

    # Scope — mirrors the portal display
    scope_type     = str(getattr(rprops, "applied_scope_type", "") or "")
    applied_scopes = getattr(rprops, "applied_scopes", None) or []

    if scope_type.lower() == "shared":
        scope_label = "Shared"
        scope_sub   = "All subscriptions"
        scope_rg    = "All resource groups"
    elif applied_scopes:
        parts   = applied_scopes[0].split("/")
        sub_idx = next((i for i, p in enumerate(parts) if p.lower() == "subscriptions"), None)
        rg_idx  = next((i for i, p in enumerate(parts) if p.lower() == "resourcegroups"), None)
        scope_sub = parts[sub_idx + 1] if sub_idx is not None and sub_idx + 1 < len(parts) else ""
        scope_rg  = parts[rg_idx + 1]  if rg_idx  is not None and rg_idx  + 1 < len(parts) else ""
        scope_label = "ResourceGroup" if scope_rg else "Single"
    else:
        scope_label = scope_type
        scope_sub   = ""
        scope_rg    = ""

    # Product name: prefer sku_description, fall back to sku.name
    sku_obj = getattr(reservation, "sku", None)
    product = (
        getattr(rprops, "sku_description", None)
        or (sku_obj.name if sku_obj else None)
        or ""
    )

    deep_link = PORTAL_RI_LINK.format(order_id=order_id, reservation_id=reservation_id)

    return {
        "Name":                     getattr(rprops, "display_name", None) or reservation_id,
        "Reservation Id":           reservation_id,
        "Reservation order Id":     order_id,
        "Status":                   str(getattr(rprops, "provisioning_state", "") or ""),
        "Expiration date":          _fmt_date(
                                        getattr(rprops, "expiry_date_time", None)
                                        or getattr(rprops, "expiry_date", None)
                                    ),
        "Purchase date":            _fmt_date(
                                        getattr(oprops, "request_date_time", None)
                                        if oprops else None
                                    ),
        "Term":                     str(getattr(rprops, "term", "") or "").upper(),
        "Scope":                    scope_label,
        "Scope subscription":       scope_sub,
        "Scope resource group":     scope_rg,
        "Type":                     str(getattr(rprops, "reserved_resource_type", "") or ""),
        "Product name":             product,
        "Region":                   str(getattr(rprops, "location", None) or getattr(reservation, "location", "") or ""),
        "Quantity":                 getattr(rprops, "quantity", "") or "",
        "Utilization % 1 Day":      utilisation.get("util_1d")  if utilisation.get("util_1d")  is not None else "",
        "Utilization % 7 Day":      utilisation.get("util_7d")  if utilisation.get("util_7d")  is not None else "",
        "Utilization % 30 Day":     utilisation.get("util_30d") if utilisation.get("util_30d") is not None else "",
        "Deep link to reservation": deep_link,
    }


# ── Fetch & export for a single token/tenant ─────────────────────────────────

def fetch_reservations_for_tenant(credential, fetch_util: bool = True) -> list[dict]:
    """Return all reservation rows for the tenant behind the given credential."""
    res_client  = _make_reservations_client(credential)
    cons_client = _make_consumption_client(credential) if fetch_util else None

    orders = list_reservation_orders(res_client)

    rows: list[dict] = []
    seen_reservations: set[str] = set()

    for order in orders:
        order_id = order.name or ""
        print(f"  Order {order_id} ...", end=" ", flush=True)

        try:
            reservations = list_reservations_in_order(res_client, order_id)
        except Exception as e:
            print(f"ERROR listing reservations: {e}")
            continue

        print(f"{len(reservations)} reservation(s)")

        for res in reservations:
            res_id = res.name or ""
            if res_id in seen_reservations:
                continue
            seen_reservations.add(res_id)

            util = (
                fetch_utilisation(cons_client, order_id, res_id)
                if cons_client
                else {"util_1d": None, "util_7d": None, "util_30d": None}
            )
            rows.append(reservation_to_row(order, res, util))

    return rows


# ── CSV output ────────────────────────────────────────────────────────────────

def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in CSV_COLUMNS})
    print(f"\n  ✓  Written {len(rows)} row(s) → {path}")


def write_json(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2)
    print(f"  ✓  Written {len(rows)} row(s) → {path}")


def default_output_path(out_dir: Path, label: str | None) -> Path:
    suffix = f"_{label}" if label else ""
    filename = f"reservedInstances{suffix}.csv"
    return out_dir / filename


# ── Main ──────────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Export Azure Reserved Instances to a CSV compatible with the RI Advisor dashboard.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "-s", "--subscription",
        metavar="SUBSCRIPTION_ID",
        help="Single subscription mode (uses current az login session).",
    )
    p.add_argument(
        "-i", "--input",
        metavar="PATH",
        default=None,
        help="Path to customer CSV file (all rows processed).",
    )
    p.add_argument(
        "--skip-login",
        action="store_true",
        help="Deprecated no-op; kept for backward compatibility.",
    )
    p.add_argument(
        "--sp-client-id",
        default=None,
        metavar="APP_ID",
        help="App Registration client ID for non-interactive service principal login.",
    )
    p.add_argument(
        "--sp-client-secret",
        default=os.environ.get("AZURE_SP_CLIENT_SECRET"),
        metavar="SECRET",
        help="Client secret for service principal login. "
             "Falls back to AZURE_SP_CLIENT_SECRET env var.",
    )
    p.add_argument(
        "--sp-certificate",
        default=None,
        metavar="CERT_PATH",
        help="Path to PEM certificate for service principal auth "
             "(alternative to --sp-client-secret).",
    )
    p.add_argument(
        "--no-utilisation",
        action="store_true",
        help="Skip fetching utilisation data (faster, but columns will be blank).",
    )
    p.add_argument(
        "--output-dir",
        metavar="DIR",
        default="./reports",
        help="Directory for the output files (default: ./reports).",
    )
    p.add_argument(
        "--output",
        metavar="FILE",
        help="Full path for the output file base (overrides --output-dir).",
    )
    p.add_argument(
        "--output-format",
        choices=("csv", "json", "both"),
        default="both",
        help="File output format: csv, json, or both (default: both).",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="Also print JSON to stdout.",
    )
    return p


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    csv_label: str | None = None
    fetch_util = not args.no_utilisation

    all_rows: list[dict] = []
    seen_tenants: set[str] = set()

    # ── CSV-batch mode ────────────────────────────────────────────────────────
    if args.input:
        tenant_map = read_customer_csv(args.input)
        csv_label = Path(args.input).stem.upper()

        for tenant_id, subs in tenant_map.items():
            if tenant_id in seen_tenants:
                continue
            seen_tenants.add(tenant_id)

            print(f"\nFetching reservations for tenant {tenant_id} ...")
            try:
                credential = get_credential(
                    tenant_id,
                    args.sp_client_id,
                    args.sp_client_secret,
                    args.sp_certificate,
                )
            except Exception as exc:
                print(f"  ⚠  Skipping tenant {tenant_id} — credential error: {exc}")
                continue
            try:
                rows = fetch_reservations_for_tenant(credential, fetch_util=fetch_util)
            except Exception as e:
                print(f"  ⚠  Error fetching reservations: {e} — skipping tenant")
                continue
            all_rows.extend(rows)
            print(f"  Tenant {tenant_id}: {len(rows)} reservation(s) collected")

    # ── Single-subscription / single-session mode ─────────────────────────────
    else:
        if args.subscription:
            print(f"Fetching reservations for subscription-scoped session: {args.subscription} ...")
        else:
            print("Fetching reservations for the current az-login session ...")
        credential = get_credential(
            None,
            args.sp_client_id,
            args.sp_client_secret,
            args.sp_certificate,
        )
        try:
            all_rows = fetch_reservations_for_tenant(credential, fetch_util=fetch_util)
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)

    # ── Output ────────────────────────────────────────────────────────────────
    if not all_rows:
        print("\nNo reservations found.")
        sys.exit(0)

    # Deduplicate by Reservation Id (multiple tenants could theoretically overlap)
    seen: set[str] = set()
    deduped: list[dict] = []
    for row in all_rows:
        rid = row.get("Reservation Id", "")
        if rid not in seen:
            seen.add(rid)
            deduped.append(row)

    print(f"\n{'═' * 70}")
    print(f"  Total reservations : {len(deduped)}")

    if args.output:
        out_path = Path(args.output)
    else:
        from datetime import date as _date
        today = _date.today().isoformat()
        label_part = csv_label if csv_label else "export"
        out_path = out_dir / f"{label_part}_reserved_instances_{today}_{today}.csv"

    json_path = out_path.with_suffix(".json")
    if args.output_format in ("csv", "both"):
        write_csv(out_path, deduped)
    if args.output_format in ("json", "both"):
        write_json(json_path, deduped)

    if args.json:
        print(json.dumps(deduped, indent=2))

    print(f"{'═' * 70}\n")


if __name__ == "__main__":
    main()
