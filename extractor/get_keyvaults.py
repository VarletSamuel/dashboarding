#!/usr/bin/env python3
"""
Azure Key Vaults – Quality & Security Audit Exporter
====================================================
Exports Key Vaults with comprehensive security, compliance, and configuration quality checks for cost optimization and governance.

Output: semicolon-delimited, BOM-prefixed CSV (Excel-compatible).

What this collects
------------------
- Vault inventory: SKU, region, creation date
- Security posture: soft delete, purge protection, public network access, private endpoints, firewall rules
- Access control: RBAC vs legacy policies, managed identities, number of assignments
- Data protection: backup enabled, autorotation configured
- TLS/HTTPS: minimum TLS version
- Logging & monitoring: diagnostic settings, Defender enabled
- Secrets/certificates: name, enabled, expiration, days until expiration, autorotation
- Derived quality signals: security score, compliance score, recommendations

Prerequisites
-------------
    pip install azure-identity azure-mgmt-keyvault azure-mgmt-network azure-mgmt-resource requests

Authentication
--------------
    Same as the other extractors: reads the customer CSV (-i), groups subscriptions by tenant, runs `az login --tenant` once per tenant (unless --skip-login is set), then iterates all matching subscriptions.

Usage
-----
    python get_keyvaults.py -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    python get_keyvaults.py -s <subscription-id> --output-dir ./reports
    python get_keyvaults.py --output-dir ./reports
"""


# ── dependency check ─────────────────────────────────────────────────────────
import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

_REQUIRED = {
    "azure.identity": "azure-identity",
    "azure.mgmt.keyvault": "azure-mgmt-keyvault",
    "azure.mgmt.resource": "azure-mgmt-resource",
    "azure.mgmt.network": "azure-mgmt-network",
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
        print(f"    ✗  {pkg}")
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
from azure.mgmt.keyvault import KeyVaultManagementClient
from azure.mgmt.network import NetworkManagementClient

# ── Argument parsing and main export skeleton ───────────────────────────────
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

def main():
    parser = argparse.ArgumentParser(
        description="Export Azure Key Vaults with security, compliance, and quality checks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
    %(prog)s -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    %(prog)s -s <subscription-id> --output-dir ./reports
    %(prog)s --output-dir ./reports
        """,
    )
    parser.add_argument(
        "-i", "--input",
        default=None,
        help="Path to JSON customer file (all rows processed)",
    )
    parser.add_argument(
        "-s", "--subscription",
        help="Single subscription ID (alternative to -i mode)",
    )
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
        "--output-format",
        choices=("csv", "json", "both"),
        default="both",
        help="File output format: csv, json, or both (default: both)",
    )
    parser.add_argument(
        "--output-dir",
        default="./reports",
        help="Directory for the output files",
    )
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d_%H%M")

    if args.input:
        prefix = os.path.splitext(os.path.basename(args.input))[0].upper()
        summary_filename = f"{prefix}_keyvaults_summary_{date_str}.csv"
        summary_json_filename = f"{prefix}_keyvaults_summary_{date_str}.json"
    else:
        summary_filename = f"keyvaults_summary_{date_str}.csv"
        summary_json_filename = f"keyvaults_summary_{date_str}.json"

    summary_path = os.path.join(args.output_dir, summary_filename)
    summary_json_path = os.path.join(args.output_dir, summary_json_filename)

    # [Vault discovery, property extraction, and export logic will be implemented here.]
    print(f"[INFO] Would export to: {summary_path} and {summary_json_path}")

if __name__ == "__main__":
    main()
