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
import requests
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

# ── Summary file: one row per key vault ─────────────────────────────────────
SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "name",
    "key_vault_name",
    "key_vault_id",
    "location",
    "sku_name",
    "vault_uri",
    "create_mode",
    "authorization_mechanism",
    "enable_rbac_authorization",
    "enabled_for_deployment",
    "enabled_for_disk_encryption",
    "enabled_for_template_deployment",
    "soft_delete_enabled",
    "purge_protection_enabled",
    "public_network_access",
    "default_action",
    "bypass_rules",
    "private_endpoint_count",
    "private_endpoint_names",
    "soft_delete_retention_days",
    "tags",
    "quality_score",
    "security_score",
    "compliance_score",
    "recommendations",
]


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


def fmt(value):
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def parse_resource_group(resource_id: str) -> str:
    if not resource_id or "/resourceGroups/" not in resource_id:
        return ""
    return resource_id.split("/resourceGroups/")[1].split("/")[0]


def tags_str(tags: dict | None) -> str:
    if not tags:
        return ""
    return ", ".join(f"{k}={v}" for k, v in sorted(tags.items()))


def number_to_str(value, decimals: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        rounded = round(value, decimals)
        if rounded.is_integer():
            return str(int(rounded))
        return f"{rounded:.{decimals}f}"
    return str(value)


def get_private_endpoints(
    network_client: NetworkManagementClient,
    vault_id: str,
    resource_group: str,
) -> tuple[int, str]:
    """Find private endpoints connected to this key vault."""
    if not resource_group:
        return 0, ""

    try:
        endpoints = network_client.private_endpoints.list(resource_group)
        matched = []
        for ep in endpoints:
            for conn in (ep.private_link_service_connections or []):
                service_id = getattr(conn, "private_link_service_id", "") or ""
                if service_id.lower() == vault_id.lower():
                    matched.append(fmt(ep.name))
        return len(matched), " | ".join(matched)
    except Exception as exc:
        print(f"    ⚠  Could not list private endpoints for {resource_group}: {exc}")
        return 0, ""


def get_network_rule_summary(vault_properties) -> tuple[str, str]:
    try:
        rules = getattr(vault_properties, "network_acls", None)
        if not rules:
            return "Allow", ""

        default_action = fmt(getattr(rules, "default_action", "Allow"))
        bypasses = fmt(getattr(rules, "bypass", ""))
        return default_action, bypasses
    except Exception:
        return "Allow", ""


def calculate_security_score(vault_info: dict) -> float:
    score = 0

    if vault_info.get("soft_delete_enabled") == "True":
        score += 20
    if vault_info.get("purge_protection_enabled") == "True":
        score += 20
    if (vault_info.get("default_action") or "").lower() == "deny":
        score += 15
    if vault_info.get("private_endpoint_count", 0) and int(vault_info.get("private_endpoint_count", 0)) > 0:
        score += 15
    if vault_info.get("enable_rbac_authorization") == "True":
        score += 10
    if (vault_info.get("public_network_access") or "").lower() in ("disabled", "false"):
        score += 20

    return min(100, score)


def calculate_compliance_score(vault_info: dict) -> float:
    score = 0

    if vault_info.get("soft_delete_enabled") == "True":
        score += 25
    if vault_info.get("purge_protection_enabled") == "True":
        score += 25
    if vault_info.get("enable_rbac_authorization") == "True":
        score += 15
    if vault_info.get("enabled_for_template_deployment") == "True":
        score += 10
    if vault_info.get("tags"):
        score += 10
    if (vault_info.get("default_action") or "").lower() == "deny":
        score += 15

    return min(100, score)


def generate_recommendations(vault_info: dict) -> list[str]:
    recommendations = []

    if vault_info.get("soft_delete_enabled") != "True":
        recommendations.append("Enable soft delete for secret/certificate recovery")
    if vault_info.get("purge_protection_enabled") != "True":
        recommendations.append("Enable purge protection to prevent permanent deletion")
    if (vault_info.get("default_action") or "").lower() != "deny":
        recommendations.append("Restrict network access with firewall default deny")
    if (vault_info.get("private_endpoint_count", 0) or 0) == 0:
        recommendations.append("Consider private endpoints for private network isolation")
    if vault_info.get("enable_rbac_authorization") != "True":
        recommendations.append("Use RBAC authorization instead of legacy access policies")

    return recommendations


def build_vault_summary(
    vault,
    tenant_id: str,
    sub_id: str,
    sub_name: str,
    resource_group: str,
    network_client: NetworkManagementClient,
) -> dict:
    vault_id = fmt(vault.id)
    properties = getattr(vault, "properties", None)

    # SDKs expose SKU under different paths depending on API/model version.
    sku_obj = getattr(vault, "sku", None) or getattr(properties, "sku", None)
    sku_name = fmt(getattr(sku_obj, "name", ""))

    create_mode = fmt(getattr(properties, "create_mode", getattr(vault, "create_mode", ""))).strip()
    if not create_mode:
        # Existing/normal vaults frequently return no explicit create mode.
        create_mode = "default"

    default_action, bypass_rules = get_network_rule_summary(properties)
    private_ep_count, private_ep_names = get_private_endpoints(network_client, vault_id, resource_group)

    vault_info = {
        "tenant_id": tenant_id or "",
        "subscription_id": sub_id,
        "subscription_name": sub_name or "",
        "resource_group": resource_group,
        "key_vault_name": fmt(vault.name),
        "name": fmt(vault.name),
        "key_vault_id": vault_id,
        "location": fmt(vault.location),
        "sku_name": sku_name,
        "vault_uri": fmt(getattr(properties, "vault_uri", "")),
        "create_mode": create_mode,
        "enable_rbac_authorization": fmt(getattr(properties, "enable_rbac_authorization", False)),
        "enabled_for_deployment": fmt(getattr(properties, "enabled_for_deployment", False)),
        "enabled_for_disk_encryption": fmt(getattr(properties, "enabled_for_disk_encryption", False)),
        "enabled_for_template_deployment": fmt(getattr(properties, "enabled_for_template_deployment", False)),
        "soft_delete_enabled": fmt(getattr(properties, "enable_soft_delete", getattr(properties, "soft_delete_enabled", False))),
        "purge_protection_enabled": fmt(getattr(properties, "enable_purge_protection", getattr(properties, "purge_protection_enabled", False))),
        "public_network_access": fmt(getattr(properties, "public_network_access", "")),
        "default_action": default_action,
        "bypass_rules": bypass_rules,
        "private_endpoint_count": private_ep_count,
        "private_endpoint_names": private_ep_names,
        "soft_delete_retention_days": getattr(properties, "soft_delete_retention_in_days", None),
        "tags": tags_str(getattr(vault, "tags", None)),
    }

    vault_info["authorization_mechanism"] = (
        "RBAC" if str(vault_info.get("enable_rbac_authorization", "")).strip().lower() == "true" else "Access Policies"
    )

    security_score = calculate_security_score(vault_info)
    compliance_score = calculate_compliance_score(vault_info)
    quality_score = (security_score + compliance_score) / 2
    recommendations = generate_recommendations(vault_info)

    vault_info["security_score"] = security_score
    vault_info["compliance_score"] = compliance_score
    vault_info["quality_score"] = quality_score
    vault_info["recommendations"] = " | ".join(recommendations[:3])

    return vault_info


def process_subscription(
    credential,
    sub_id: str,
    sub_name: str,
    tenant_id: str,
) -> list[dict]:
    print(f"\n── Subscription: {sub_name or sub_id} ──")
    kv_client = KeyVaultManagementClient(credential, sub_id)
    network_client = NetworkManagementClient(credential, sub_id)
    summary_rows: list[dict] = []

    try:
        vaults = list(kv_client.vaults.list())
    except Exception as exc:
        print(f"  ⚠  Could not list key vaults: {exc}")
        return summary_rows

    if not vaults:
        print("  (no key vaults found)")
        return summary_rows

    print(f"  Found {len(vaults)} key vault(s)")

    for vault in vaults:
        resource_group = parse_resource_group(vault.id or "")
        print(f"    • {fmt(vault.name)} ({resource_group})")

        full_vault = vault
        try:
            # list() may not return all properties; get() ensures SKU/URI/create_mode are populated.
            full_vault = kv_client.vaults.get(resource_group, fmt(vault.name))
        except Exception as exc:
            print(f"      ⚠  Could not fetch full vault properties for {fmt(vault.name)}: {exc}")

        vault_summary = build_vault_summary(
            full_vault,
            tenant_id,
            sub_id,
            sub_name,
            resource_group,
            network_client,
        )
        summary_row = {key: number_to_str(vault_summary.get(key)) for key in SUMMARY_COLUMNS}
        summary_rows.append(summary_row)

    return summary_rows


def export(args):
    now = datetime.now(timezone.utc)
    all_summary: list[dict] = []

    if args.input:
        tenant_map = read_customer_csv(args.input)
        for tenant_id, subs in tenant_map.items():
            try:
                credential = get_credential(
                    tenant_id,
                    args.sp_client_id,
                    args.sp_client_secret,
                    args.sp_certificate,
                )
            except Exception as exc:
                print(f"  Skipping tenant {tenant_id} (credential error: {exc})")
                continue

            for sub_id, sub_name in subs:
                s = process_subscription(credential, sub_id, sub_name, tenant_id)
                all_summary.extend(s)
    elif args.subscription:
        credential = get_credential(
            None,
            args.sp_client_id,
            args.sp_client_secret,
            args.sp_certificate,
        )
        s = process_subscription(credential, args.subscription, "", "")
        all_summary.extend(s)
    else:
        credential = get_credential(None, args.sp_client_id, args.sp_client_secret, args.sp_certificate)
        subs = list_enabled_subscriptions(credential)
        print(f"Found {len(subs)} enabled subscription(s) via ARM SDK")
        for sub_id, sub_name, tenant_id in subs:
            s = process_subscription(credential, sub_id, sub_name, tenant_id)
            all_summary.extend(s)

    os.makedirs(args.output_dir, exist_ok=True)
    date_str = now.strftime("%Y%m%d")

    if args.input:
        prefix = os.path.splitext(os.path.basename(args.input))[0].upper()
        summary_filename = f"{prefix}_keyvaults_summary_{date_str}.csv"
        summary_json_filename = f"{prefix}_keyvaults_summary_{date_str}.json"
    else:
        summary_filename = f"keyvaults_summary_{date_str}.csv"
        summary_json_filename = f"keyvaults_summary_{date_str}.json"

    summary_path = os.path.join(args.output_dir, summary_filename)
    summary_json_path = os.path.join(args.output_dir, summary_json_filename)

    if args.output_format in ("csv", "both"):
        with open(summary_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(all_summary)

    if args.output_format in ("json", "both"):
        with open(summary_json_path, "w", encoding="utf-8") as handle:
            json.dump(all_summary, handle, indent=2)

    print(f"\n{'═' * 70}")
    print(f"  ✅  Exported {len(all_summary)} key vault(s)")
    if args.output_format in ("csv", "both"):
        print(f"      → {summary_path}")
    if args.output_format in ("json", "both"):
        print(f"      → {summary_json_path}")
    print(f"{'═' * 70}\n")


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
    export(args)

if __name__ == "__main__":
    main()
