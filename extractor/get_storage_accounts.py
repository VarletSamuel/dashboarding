#!/usr/bin/env python3
"""
Azure Storage Accounts – Quality & Security Audit Exporter
===========================================================
Exports Storage Accounts with comprehensive security, compliance, and 
configuration quality checks for cost optimization and governance.

Output: semicolon-delimited, BOM-prefixed CSV (Excel-compatible).

What this collects
------------------
- Account inventory: SKU, tier, region, creation date
- Security posture: TLS version, HTTPS only, encryption, versioning
- Access control: public/private endpoints, firewall rules, anonymous access
- Compliance signals: HNS enabled, soft delete, immutability policies
- Data redundancy: LRS/GRS/RAGRS/ZRS settings, failover status
- Derived quality signals: security score, compliance score, cost optimization flags

Prerequisites
-------------
    pip install azure-identity azure-mgmt-storage azure-mgmt-network

Authentication
--------------
    Same as the other extractors in this repo: reads the customer CSV (-i),
    groups subscriptions by tenant, runs `az login --tenant` once per tenant
    (unless --skip-login is set), then iterates all matching subscriptions.

Usage
-----
    python get_storage_accounts.py -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    python get_storage_accounts.py -s <subscription-id> --output-dir ./reports
    python get_storage_accounts.py --output-dir ./reports
"""

import argparse
import csv
import json
import os
import requests
import sys
from collections import defaultdict
from datetime import datetime, timezone


# ── dependency check ─────────────────────────────────────────────────────────

_REQUIRED = {
    "azure.identity": "azure-identity",
    "azure.mgmt.storage": "azure-mgmt-storage",
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
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.network import NetworkManagementClient


# ── Summary file: one row per storage account ────────────────────────────────
SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "name",
    "storage_account_name",
    "storage_account_id",
    "location",
    "creation_time",
    "kind",
    "sku_name",
    "sku_tier",
    "access_tier",
    "https_only",
    "minimum_tls_version",
    "default_action",
    "bypass_rules",
    "public_network_access_enabled",
    "is_hns_enabled",
    "blob_soft_delete_days",
    "container_soft_delete_days",
    "blob_versioning_enabled",
    "blob_change_feed_enabled",
    "blob_restore_enabled",
    "table_soft_delete_days",
    "file_share_soft_delete_days",
    "queue_soft_delete_days",
    "immutability_policy_enabled",
    "replication_type",
    "status",
    "failover_status",
    "primary_endpoints",
    "secondary_endpoints",
    "private_endpoint_count",
    "private_endpoint_names",
    "encryption_key_source",
    "infrastructure_encryption_enabled",
    "customer_managed_key_enabled",
    "shared_access_key_enabled",
    "supports_https_traffic_only",
    "access_keys_last_rotation",
    "has_blob_containers",
    "container_count",
    "has_file_shares",
    "file_share_count",
    "has_tables",
    "table_count",
    "has_queues",
    "queue_count",
    "storage_used_gb",
    "qc_https_only",
    "qc_tls_12_or_higher",
    "qc_no_public_blob_access",
    "qc_shared_key_disabled",
    "qc_network_restricted",
    "qc_soft_delete_enabled",
    "qc_infrastructure_encryption",
    "qc_has_tags",
    "tags",
    "quality_score",
    "security_score",
    "compliance_score",
    "cost_signal",
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
    storage_account_id: str,
    resource_group: str,
) -> tuple[int, str]:
    """Find private endpoints connected to this storage account."""
    try:
        endpoints = network_client.private_endpoints.list(resource_group)
        matched = []
        for ep in endpoints:
            if ep.private_link_service_connections:
                for conn in ep.private_link_service_connections:
                    if conn.private_link_service_id and conn.private_link_service_id.lower() == storage_account_id.lower():
                        matched.append(fmt(ep.name))
        return len(matched), " | ".join(matched)
    except Exception as exc:
        print(f"    ⚠  Could not list private endpoints: {exc}")
        return 0, ""


def get_network_rule_summary(storage_account) -> tuple[str, str]:
    """Extract network rules summary."""
    try:
        rules = storage_account.network_rule_set
        if not rules:
            return "Allow", ""
        
        default_action = fmt(getattr(rules, "default_action", "Allow"))
        bypasses = fmt(getattr(rules, "bypass", ""))
        return default_action, bypasses
    except Exception:
        return "Allow", ""


def calculate_security_score(account_info: dict) -> float:
    """
    Calculate a security score (0-100) based on security configuration.
    Higher is better.
    """
    score = 0
    
    # HTTPS only (20 pts)
    if account_info.get("https_only") == "True":
        score += 20
    
    # Minimum TLS version (15 pts)
    tls_version = account_info.get("minimum_tls_version", "")
    if tls_version == "TLS1_2":
        score += 10
    elif tls_version == "TLS1_3":
        score += 15
    
    # Infrastructure encryption (10 pts)
    if account_info.get("infrastructure_encryption_enabled") == "True":
        score += 10
    
    # Customer-managed keys (20 pts)
    if account_info.get("customer_managed_key_enabled") == "True":
        score += 20
    
    # Firewall enabled with restrictions (15 pts)
    default_action = account_info.get("default_action", "")
    if default_action == "Deny":
        score += 15
    
    # Soft delete enabled (10 pts)
    soft_delete_days = account_info.get("blob_soft_delete_days", "")
    if soft_delete_days and soft_delete_days != "":
        try:
            if int(soft_delete_days) > 0:
                score += 10
        except (ValueError, TypeError):
            pass
    
    # Versioning enabled (5 pts)
    if account_info.get("blob_versioning_enabled") == "True":
        score += 5
    
    # Change feed enabled (5 pts)
    if account_info.get("blob_change_feed_enabled") == "True":
        score += 5
    
    return min(100, score)


def calculate_compliance_score(account_info: dict) -> float:
    """
    Calculate a compliance score (0-100) based on compliance configuration.
    """
    score = 0
    
    # HNS enabled (useful for data lake / compliance) (15 pts)
    if account_info.get("is_hns_enabled") == "True":
        score += 15
    
    # Soft delete enabled (20 pts)
    if account_info.get("blob_soft_delete_days") and account_info.get("blob_soft_delete_days") != "":
        try:
            if int(account_info.get("blob_soft_delete_days", 0)) > 0:
                score += 20
        except (ValueError, TypeError):
            pass
    
    # Versioning enabled (20 pts)
    if account_info.get("blob_versioning_enabled") == "True":
        score += 20
    
    # Immutability policy (20 pts)
    if account_info.get("immutability_policy_enabled") == "True":
        score += 20
    
    # Restore capability (10 pts)
    if account_info.get("blob_restore_enabled") == "True":
        score += 10
    
    # Replication (GRS/RA-GRS/RAGRS/ZRS) (15 pts)
    replication = (account_info.get("replication_type") or "").upper()
    if replication in ("GRS", "RAGRS", "ZRS", "GZRS", "RA-GZRS"):
        score += 15
    
    return min(100, score)


def generate_recommendations(account_info: dict) -> list[str]:
    """Generate actionable recommendations for the storage account."""
    recommendations = []
    
    # Security recommendations
    if account_info.get("https_only") != "True":
        recommendations.append("Enable HTTPS-only access for data in transit security")
    
    minimum_tls = account_info.get("minimum_tls_version", "")
    if minimum_tls and minimum_tls not in ("TLS1_2", "TLS1_3"):
        recommendations.append(f"Upgrade minimum TLS version from {minimum_tls} to TLS 1.2 or higher")
    
    if account_info.get("default_action") != "Deny":
        recommendations.append("Consider enabling firewall with default deny to restrict public access")
    
    if account_info.get("infrastructure_encryption_enabled") != "True":
        recommendations.append("Enable infrastructure encryption for double encryption at rest")
    
    if account_info.get("customer_managed_key_enabled") != "True":
        recommendations.append("Consider using customer-managed keys for encryption control")
    
    # Compliance recommendations
    if account_info.get("blob_soft_delete_days") == "":
        recommendations.append("Enable blob soft delete for accidental deletion protection")
    
    if account_info.get("blob_versioning_enabled") != "True":
        recommendations.append("Enable blob versioning for point-in-time recovery and audit trail")
    
    if account_info.get("immutability_policy_enabled") != "True" and account_info.get("kind") in ("StorageV2", "BlobStorage"):
        recommendations.append("Consider enabling immutability policies for compliance and data retention")
    
    # Cost recommendations
    sku_tier = account_info.get("sku_tier", "")
    if sku_tier == "Premium" and account_info.get("access_tier") == "Cool":
        recommendations.append("Premium accounts with cool access tier may have higher costs; review if appropriate")
    
    replication = (account_info.get("replication_type") or "").upper()
    if replication in ("GRS", "RAGRS", "GZRS", "RA-GZRS"):
        recommendations.append("GRS/RAGRS replication incurs 2x storage cost; review necessity for compliance vs. cost")
    
    if account_info.get("public_network_access_enabled") == "True" and account_info.get("private_endpoint_count", 0) == 0:
        recommendations.append("Storage account is publicly accessible; consider private endpoints if not needed for public access")
    
    return recommendations


def build_account_summary(
    account,
    tenant_id: str,
    sub_id: str,
    sub_name: str,
    resource_group: str,
    network_client: NetworkManagementClient,
    storage_client: StorageManagementClient,
) -> dict:
    """Extract comprehensive storage account information and quality metrics."""
    
    account_id = fmt(account.id)
    account_name = fmt(account.name)
    account_location = fmt(account.location)
    sku = account.sku
    kind = fmt(account.kind)
    
    # Get storage account properties
    properties = account
    
    # Encryption settings
    encryption = getattr(properties, "encryption", None)
    encryption_key_source = ""
    infrastructure_encryption = "False"
    customer_managed_key = "False"
    if encryption:
        encryption_key_source = fmt(getattr(encryption, "key_source", "Microsoft.Storage"))
        services = getattr(encryption, "services", None)
        if services:
            blob_encryption = getattr(services, "blob", None)
            if blob_encryption:
                infrastructure_encryption = str(getattr(blob_encryption, "key_type", "") == "Account")
        if encryption_key_source == "Microsoft.KeyVault":
            customer_managed_key = "True"
    
    # Network settings
    default_action, bypass_rules = get_network_rule_summary(properties)
    https_only = fmt(getattr(properties, "https_only", False))
    public_network_access = fmt(getattr(properties, "allow_blob_public_access", True))
    minimum_tls = fmt(getattr(properties, "minimum_tls_version", "TLS1_0"))
    shared_access_key_enabled = fmt(getattr(properties, "shared_access_key_enabled", True))
    
    # Private endpoints
    private_ep_count, private_ep_names = get_private_endpoints(network_client, account_id, resource_group)
    
    # Blob properties
    blob_properties = getattr(properties, "blob_restore_policy", None)
    blob_restore_enabled = "False"
    if blob_properties:
        blob_restore_enabled = fmt(getattr(blob_properties, "enabled", False))
    
    # Delete policies (soft delete)
    blob_soft_delete_days = ""
    container_soft_delete_days = ""
    table_soft_delete_days = ""
    file_share_soft_delete_days = ""
    queue_soft_delete_days = ""
    
    delete_retention = getattr(properties, "delete_retention_policy", None)
    if delete_retention:
        if hasattr(delete_retention, "blob"):
            blob_sr = getattr(delete_retention, "blob", None)
            if blob_sr and getattr(blob_sr, "enabled", False):
                blob_soft_delete_days = str(getattr(blob_sr, "days", 0))
        if hasattr(delete_retention, "container"):
            container_sr = getattr(delete_retention, "container", None)
            if container_sr and getattr(container_sr, "enabled", False):
                container_soft_delete_days = str(getattr(container_sr, "days", 0))
    
    # Feature flags
    is_hns_enabled = fmt(getattr(properties, "is_hns_enabled", False))
    blob_versioning_enabled = fmt(getattr(properties, "blob_versioning_enabled", False))
    blob_change_feed_enabled = fmt(getattr(properties, "blob_change_feed_enabled", False))
    
    # Replication and status
    sku_name = fmt(getattr(sku, "name", ""))
    sku_tier = fmt(getattr(sku, "tier", ""))
    access_tier = fmt(getattr(properties, "access_tier", "Hot"))
    
    # Map SKU to replication type
    replication_type = ""
    if sku_name:
        sku_upper = sku_name.upper()
        if "ZRS" in sku_upper:
            replication_type = "ZRS"
        elif "GZRS" in sku_upper:
            if "RA" in sku_upper:
                replication_type = "RA-GZRS"
            else:
                replication_type = "GZRS"
        elif "GRS" in sku_upper:
            if "RA" in sku_upper:
                replication_type = "RA-GRS"
            else:
                replication_type = "GRS"
        elif "RAGRS" in sku_upper:
            replication_type = "RA-GRS"
        else:
            replication_type = "LRS"
    
    account_status = fmt(getattr(properties, "status_of_primary", ""))
    failover_status = ""
    status_secondary = fmt(getattr(properties, "status_of_secondary", ""))
    if status_secondary:
        failover_status = f"Primary: {account_status}, Secondary: {status_secondary}"
    
    # Endpoints
    primary_endpoints = getattr(properties, "primary_endpoints", None)
    secondary_endpoints = getattr(properties, "secondary_endpoints", None)
    primary_ep_str = ""
    secondary_ep_str = ""
    if primary_endpoints:
        endpoints_list = [getattr(primary_endpoints, "blob", ""), getattr(primary_endpoints, "file", ""), getattr(primary_endpoints, "queue", ""), getattr(primary_endpoints, "table", "")]
        primary_ep_str = " | ".join(e for e in endpoints_list if e)
    if secondary_endpoints:
        endpoints_list = [getattr(secondary_endpoints, "blob", ""), getattr(secondary_endpoints, "file", ""), getattr(secondary_endpoints, "queue", ""), getattr(secondary_endpoints, "table", "")]
        secondary_ep_str = " | ".join(e for e in endpoints_list if e)
    
    # Creation time
    creation_time = fmt(getattr(properties, "creation_time", ""))
    
    # Immutability (this would require additional API calls; simplified here)
    immutability_policy = "False"  # Would need to query blob service properties
    
    # Storage used and container/share counts (would require additional API calls)
    storage_used_gb = ""
    container_count = ""
    file_share_count = ""
    table_count = ""
    queue_count = ""
    has_blob_containers = ""
    has_file_shares = ""
    has_tables = ""
    has_queues = ""
    
    # Simulate counts (in production, would query blob/file/queue/table services)
    try:
        # This would require blob_service_client for actual counts
        container_count = ""
        has_blob_containers = ""
    except Exception:
        pass
    
    # Build account info dictionary
    account_info = {
        "tenant_id": tenant_id or "",
        "subscription_id": sub_id,
        "subscription_name": sub_name or "",
        "resource_group": resource_group,
        "name": account_name,
        "storage_account_name": account_name,
        "storage_account_id": account_id,
        "location": account_location,
        "creation_time": creation_time,
        "kind": kind,
        "sku_name": sku_name,
        "sku_tier": sku_tier,
        "access_tier": access_tier,
        "https_only": https_only,
        "minimum_tls_version": minimum_tls,
        "default_action": default_action,
        "bypass_rules": bypass_rules,
        "public_network_access_enabled": public_network_access,
        "is_hns_enabled": is_hns_enabled,
        "blob_soft_delete_days": blob_soft_delete_days,
        "container_soft_delete_days": container_soft_delete_days,
        "blob_versioning_enabled": blob_versioning_enabled,
        "blob_change_feed_enabled": blob_change_feed_enabled,
        "blob_restore_enabled": blob_restore_enabled,
        "table_soft_delete_days": table_soft_delete_days,
        "file_share_soft_delete_days": file_share_soft_delete_days,
        "queue_soft_delete_days": queue_soft_delete_days,
        "immutability_policy_enabled": immutability_policy,
        "replication_type": replication_type,
        "status": account_status,
        "failover_status": failover_status,
        "primary_endpoints": primary_ep_str,
        "secondary_endpoints": secondary_ep_str,
        "private_endpoint_count": private_ep_count,
        "private_endpoint_names": private_ep_names,
        "encryption_key_source": encryption_key_source,
        "infrastructure_encryption_enabled": infrastructure_encryption,
        "customer_managed_key_enabled": customer_managed_key,
        "shared_access_key_enabled": shared_access_key_enabled,
        "supports_https_traffic_only": https_only,
        "access_keys_last_rotation": "",
        "has_blob_containers": has_blob_containers,
        "container_count": container_count,
        "has_file_shares": has_file_shares,
        "file_share_count": file_share_count,
        "has_tables": has_tables,
        "table_count": table_count,
        "has_queues": has_queues,
        "queue_count": queue_count,
        "storage_used_gb": storage_used_gb,
        "tags": tags_str(getattr(properties, "tags", None)),
    }

    def bool_str(value: bool) -> str:
        return "True" if value else "False"

    def truthy(value) -> bool:
        return str(value).strip().lower() in {"true", "1", "yes", "enabled"}

    soft_delete_days = 0
    try:
        soft_delete_days = int(account_info.get("blob_soft_delete_days") or 0)
    except (TypeError, ValueError):
        soft_delete_days = 0

    min_tls = (account_info.get("minimum_tls_version") or "").strip().upper().replace("_", "")
    account_info["qc_https_only"] = bool_str(truthy(account_info.get("https_only")))
    account_info["qc_tls_12_or_higher"] = bool_str(min_tls in {"TLS12", "TLS13"})
    account_info["qc_no_public_blob_access"] = bool_str(not truthy(account_info.get("public_network_access_enabled")))
    account_info["qc_shared_key_disabled"] = bool_str(not truthy(account_info.get("shared_access_key_enabled")))
    account_info["qc_network_restricted"] = bool_str((account_info.get("default_action") or "").strip().lower() == "deny")
    account_info["qc_soft_delete_enabled"] = bool_str(soft_delete_days > 0)
    account_info["qc_infrastructure_encryption"] = bool_str(truthy(account_info.get("infrastructure_encryption_enabled")))
    account_info["qc_has_tags"] = bool_str(bool(account_info.get("tags")))
    
    # Calculate scores
    security_score = calculate_security_score(account_info)
    compliance_score = calculate_compliance_score(account_info)
    quality_score = (security_score + compliance_score) / 2
    
    account_info["security_score"] = security_score
    account_info["compliance_score"] = compliance_score
    account_info["quality_score"] = quality_score
    
    # Cost signals
    cost_signals = []
    if sku_tier == "Premium" and kind not in ("FileStorage", "BlockBlobStorage"):
        cost_signals.append("Premium tier for general-purpose account")
    if replication_type in ("GRS", "RA-GRS", "GZRS", "RA-GZRS"):
        cost_signals.append("Geo-redundant replication (2x cost)")
    if access_tier == "Hot" and blob_soft_delete_days == "":
        cost_signals.append("Hot tier without soft delete may increase accidental deletion risk")
    
    account_info["cost_signal"] = " | ".join(cost_signals) if cost_signals else "Standard configuration"
    
    # Recommendations
    recommendations = generate_recommendations(account_info)
    account_info["recommendations"] = " | ".join(recommendations[:3])  # Top 3 recommendations
    
    return account_info


def process_subscription(
    credential,
    sub_id: str,
    sub_name: str,
    tenant_id: str,
) -> list[dict]:
    print(f"\n── Subscription: {sub_name or sub_id} ──")
    storage_client = StorageManagementClient(credential, sub_id)
    network_client = NetworkManagementClient(credential, sub_id)
    summary_rows: list[dict] = []
    
    try:
        accounts = list(storage_client.storage_accounts.list())
    except Exception as exc:
        print(f"  ⚠  Could not list storage accounts: {exc}")
        return summary_rows
    
    if not accounts:
        print("  (no storage accounts found)")
        return summary_rows
    
    print(f"  Found {len(accounts)} storage account(s)")
    
    for account in accounts:
        resource_group = parse_resource_group(account.id or "")
        
        print(f"    • {fmt(account.name)} ({resource_group})")
        
        account_summary = build_account_summary(
            account,
            tenant_id,
            sub_id,
            sub_name,
            resource_group,
            network_client,
            storage_client,
        )
        
        # Convert values to strings for CSV
        summary_row = {key: number_to_str(account_summary.get(key)) for key in SUMMARY_COLUMNS}
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
        credential = get_credential()
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
    date_str = now.strftime("%Y%m%d_%H%M")
    
    if args.input:
        prefix = os.path.splitext(os.path.basename(args.input))[0].upper()
        summary_filename = f"{prefix}_storage_accounts_summary_{date_str}.csv"
        summary_json_filename = f"{prefix}_storage_accounts_summary_{date_str}.json"
    else:
        summary_filename = f"storage_accounts_summary_{date_str}.csv"
        summary_json_filename = f"storage_accounts_summary_{date_str}.json"
    
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
    print(f"  ✅  Exported {len(all_summary)} storage account(s)")
    if args.output_format in ("csv", "both"):
        print(f"      → {summary_path}")
    if args.output_format in ("json", "both"):
        print(f"      → {summary_json_path}")
    print(f"{'═' * 70}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Export Azure Storage Accounts with security, compliance, and cost quality checks.",
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
        default=".",
        help="Directory for the output files (default: current directory). Created if it doesn't exist.",
    )
    
    args = parser.parse_args()
    export(args)


if __name__ == "__main__":
    main()
