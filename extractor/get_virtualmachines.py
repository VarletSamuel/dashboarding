#!/usr/bin/env python3
"""
Azure Virtual Machines – Cost & Rightsizing Exporter
=====================================================
Exports Virtual Machine inventory together with real Azure Monitor metrics
so you can identify deallocated VMs, stopped-but-billed VMs, low-utilisation
instances, and oversized SKUs.

Output: semicolon-delimited, BOM-prefixed CSV (Excel-compatible).

What this collects
------------------
- VM inventory: size, OS type, power state, disk config, availability zone /
  set, managed identity, extensions, NIC count, tags
- VM metrics: CPU %, Network in/out, Disk read/write bytes, available memory
- Derived cost signals: deallocated, stopped-billed, low-utilisation,
  oversized-premium, scale-down candidate, or review

Prerequisites
-------------
    pip install azure-identity azure-mgmt-compute azure-mgmt-monitor

Authentication
--------------
    Same as the other extractors: reads the customer CSV (-i), groups
    subscriptions by tenant, runs `az login --tenant` once per tenant
    (unless --skip-login is set), then iterates all matching subscriptions.

Usage
-----
    python get_virtualmachines.py -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    python get_virtualmachines.py -i ../customers/CUST.csv --lookback PT168H --output-dir ./reports/CUST
    python get_virtualmachines.py -s <subscription-id> --from 2026-01-01 --to 2026-03-31 --output-dir ./reports
"""

import argparse
import csv
import json
import os
import requests
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


# ── dependency check ─────────────────────────────────────────────────────────

_REQUIRED = {
    "azure.identity": "azure-identity",
    "azure.mgmt.compute": "azure-mgmt-compute",
    "azure.mgmt.monitor": "azure-mgmt-monitor",
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
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.monitor import MonitorManagementClient


# ── Summary file: one row per VM ──────────────────────────────────────────────
SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "vm_name",
    "vm_id",
    "location",
    "vm_size",
    "os_type",
    "os_disk_sku",
    "os_disk_size_gb",
    "data_disk_count",
    "data_disk_total_size_gb",
    "nic_count",
    "has_public_ip",
    "public_ip_addresses",
    "availability_zone",
    "availability_set",
    "proximity_placement_group",
    "ultra_ssd_enabled",
    "encryption_at_host",
    "secure_boot_enabled",
    "vtpm_enabled",
    "license_type",
    "hybrid_benefit_active",
    "hybrid_benefit_description",
    "priority",
    "eviction_policy",
    "identity_type",
    "extensions",
    "power_state",
    "provisioning_state",
    "backup_configured",
    "backup_status",
    "backup_last_run_utc",
    "tags",
    "cost_signal_category",
    "cost_signal_reason",
    "observed_sample_count",
    "cpu_avg_pct_window",
    "cpu_p95_pct_window",
    "cpu_peak_pct_window",
    "network_in_total_bytes_window",
    "network_out_total_bytes_window",
    "disk_read_total_bytes_window",
    "disk_write_total_bytes_window",
    "available_memory_avg_bytes_window",
    "available_memory_min_bytes_window",
    "managed_by",
]

# ── Timeseries file: one row per VM × timestamp ───────────────────────────────
TIMESERIES_COLUMNS = [
    "vm_id",
    "timestamp",
    "cpu_percentage",
    "network_in_bytes",
    "network_out_bytes",
    "disk_read_bytes",
    "disk_write_bytes",
    "available_memory_bytes",
]


VM_METRICS = [
    {"azure_name": "Percentage CPU",         "column": "cpu_percentage",          "aggregation": "Average"},
    {"azure_name": "Network In Total",        "column": "network_in_bytes",         "aggregation": "Total"},
    {"azure_name": "Network Out Total",       "column": "network_out_bytes",        "aggregation": "Total"},
    {"azure_name": "Disk Read Bytes",         "column": "disk_read_bytes",          "aggregation": "Total"},
    {"azure_name": "Disk Write Bytes",        "column": "disk_write_bytes",         "aggregation": "Total"},
    {"azure_name": "Available Memory Bytes",  "column": "available_memory_bytes",   "aggregation": "Average"},
]

# VM sizes that are "premium-tier" for the oversized signal
_PREMIUM_SIZE_PREFIXES = (
    "standard_e", "standard_m", "standard_g", "standard_gs",
    "standard_ls", "standard_nc", "standard_nv", "standard_nd",
    "standard_hb", "standard_hc", "standard_h",
)


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


def parse_resource_name(resource_id: str) -> str:
    if not resource_id:
        return ""
    return resource_id.rstrip("/").split("/")[-1]


def arm_list(token: str, url: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {token}"}
    items: list[dict] = []
    next_url = url
    while next_url:
        response = requests.get(next_url, headers=headers, timeout=120)
        response.raise_for_status()
        payload = response.json()
        items.extend(payload.get("value", []))
        next_url = payload.get("nextLink")
    return items


def get_nic_public_ip_names(token: str, nic_id: str, cache: dict[str, list[str]]) -> list[str]:
    if not nic_id:
        return []
    if nic_id in cache:
        return cache[nic_id]

    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://management.azure.com{nic_id}?api-version=2023-09-01"

    try:
        response = requests.get(url, headers=headers, timeout=120)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"    ⚠  Could not resolve NIC public IPs for {nic_id}: {exc}")
        cache[nic_id] = []
        return []

    names: list[str] = []
    ip_configs = ((payload.get("properties") or {}).get("ipConfigurations") or [])
    for cfg in ip_configs:
        public_ip = ((cfg.get("properties") or {}).get("publicIPAddress") or {})
        public_ip_id = public_ip.get("id")
        if public_ip_id:
            names.append(parse_resource_name(public_ip_id))

    deduped = sorted(set(n for n in names if n))
    cache[nic_id] = deduped
    return deduped


def get_vm_public_ip_info(token: str, nic_refs: list, cache: dict[str, list[str]]) -> tuple[bool, str]:
    names: list[str] = []
    for nic_ref in nic_refs or []:
        nic_id = fmt(getattr(nic_ref, "id", None))
        if not nic_id:
            continue
        names.extend(get_nic_public_ip_names(token, nic_id, cache))

    deduped = sorted(set(n for n in names if n))
    return (len(deduped) > 0, ", ".join(deduped))


def build_backup_index(token: str, subscription_id: str) -> dict[str, dict]:
    backup_index: dict[str, dict] = {}
    vaults_url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        "/providers/Microsoft.RecoveryServices/vaults?api-version=2023-02-01"
    )

    try:
        vaults = arm_list(token, vaults_url)
    except Exception as exc:
        print(f"  ⚠  Could not list Recovery Services vaults: {exc}")
        return backup_index

    if vaults:
        print(f"  Found {len(vaults)} Recovery Services vault(s)")

    for vault in vaults:
        vault_id = str(vault.get("id") or "")
        vault_name = str(vault.get("name") or "")
        rg_name = parse_resource_group(vault_id)
        if not vault_name or not rg_name:
            continue

        items_url = (
            f"https://management.azure.com/subscriptions/{subscription_id}"
            f"/resourceGroups/{rg_name}/providers/Microsoft.RecoveryServices/vaults/{vault_name}"
            "/backupProtectedItems?api-version=2023-02-01"
        )

        try:
            protected_items = arm_list(token, items_url)
        except Exception as exc:
            print(f"    ⚠  Could not list backup protected items for vault {vault_name}: {exc}")
            continue

        for item in protected_items:
            props = item.get("properties") or {}

            # Different API shapes expose VM identity with different field names.
            source_id = str(
                props.get("sourceResourceId")
                or props.get("virtualMachineId")
                or props.get("sourceResourceID")
                or ""
            ).strip()
            if not source_id:
                continue

            source_key = source_id.lower()
            last_run = str(
                props.get("lastBackupTime")
                or props.get("lastRecoveryPoint")
                or props.get("lastBackupTimeInUTC")
                or ""
            )
            status = str(
                props.get("lastBackupStatus")
                or props.get("protectionState")
                or props.get("protectionStatus")
                or "Configured"
            )

            existing = backup_index.get(source_key)
            if not existing or (last_run and last_run > str(existing.get("backup_last_run_utc") or "")):
                backup_index[source_key] = {
                    "backup_configured": True,
                    "backup_status": status,
                    "backup_last_run_utc": last_run,
                }

    if backup_index:
        print(f"  Backup index mapped to {len(backup_index)} VM resource ID(s)")

    return backup_index


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


def percentile(values: list[float], pct: float) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    rank = (len(clean) - 1) * (pct / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(clean) - 1)
    weight = rank - lower
    return clean[lower] * (1 - weight) + clean[upper] * weight


def get_power_state(compute_client: ComputeManagementClient, resource_group: str, vm_name: str) -> str:
    try:
        instance_view = compute_client.virtual_machines.instance_view(resource_group, vm_name)
        for status in (instance_view.statuses or []):
            code = (status.code or "").lower()
            if code.startswith("powerstate/"):
                return code.split("/", 1)[1]
    except Exception:
        pass
    return ""


def summarize_metric_values(values: list[float], metric_name: str) -> dict:
    if not values:
        return {"samples": 0, "avg": None, "p95": None, "peak": None, "min": None, "total": None}

    summary = {
        "samples": len(values),
        "avg": sum(values) / len(values),
        "p95": percentile(values, 95),
        "peak": max(values),
        "min": min(values),
        "total": None,
    }
    if metric_name in {"network_in_bytes", "network_out_bytes", "disk_read_bytes", "disk_write_bytes"}:
        summary["total"] = sum(values)
    return summary


def describe_hybrid_benefit(vm_info: dict, metric_summaries: dict) -> tuple[bool, str]:
    """
    Determine if Azure Hybrid Benefit is active and provide description.
    Returns (is_active: bool, description: str)
    """
    os_type = (vm_info.get("os_type") or "").lower()
    license_type = (vm_info.get("license_type") or "").strip()
    power_state = (vm_info.get("power_state") or "").lower()

    if not license_type:
        if os_type == "windows" and power_state == "running":
            return (False, "Windows VM without Azure Hybrid Benefit (AHB) — eligible for license mobility")
        return (False, "No hybrid benefit applied")

    if license_type == "Windows_Server":
        return (True, "Azure Hybrid Benefit: Windows Server license mobility")
    elif license_type == "Windows_Client":
        return (True, "Azure Hybrid Benefit: Windows Client license (BYOL)")
    elif license_type == "RHEL_BYOS":
        return (True, "BYOS: Red Hat Enterprise Linux (bring-your-own-subscription)")
    elif license_type == "SLES_BYOS":
        return (True, "BYOS: SUSE Linux Enterprise Server (bring-your-own-subscription)")
    else:
        return (True, f"License type: {license_type}")


def build_cost_signal(vm_info: dict, metric_summaries: dict, ahb_active: bool) -> tuple[str, str]:
    power_state = (vm_info.get("power_state") or "").lower()
    vm_size = (vm_info.get("vm_size") or "").lower()
    os_type = (vm_info.get("os_type") or "").lower()

    cpu_p95 = metric_summaries["cpu_percentage"]["p95"]
    cpu_peak = metric_summaries["cpu_percentage"]["peak"]

    is_premium = any(vm_size.startswith(pfx) for pfx in _PREMIUM_SIZE_PREFIXES)

    if power_state == "deallocated":
        return (
            "deallocated",
            "VM is deallocated. Compute charges have stopped but managed disk costs continue.",
        )

    if power_state in ("stopped", "stopping"):
        return (
            "stopped-billed",
            "VM is stopped but NOT deallocated. Full compute charges still apply. Deallocate to stop billing.",
        )

    if power_state not in ("running", ""):
        return (
            "non-running",
            f"VM is in power state '{power_state}'. Verify whether this is intentional.",
        )

    # Check for Windows VMs without AHB
    if os_type == "windows" and not ahb_active:
        return (
            "missing-ahb",
            "Windows VM without Azure Hybrid Benefit. Apply AHB for licensing cost savings.",
        )

    if (
        is_premium
        and (cpu_p95 is None or cpu_p95 < 15)
        and (cpu_peak is None or cpu_peak < 30)
    ):
        return (
            "oversized-premium",
            "Premium VM size with very low sustained and peak CPU utilisation. Consider downsizing or using a general-purpose SKU.",
        )

    if cpu_p95 is not None and cpu_p95 < 10 and (cpu_peak is None or cpu_peak < 25):
        return (
            "low-utilisation",
            "Consistently low CPU across the observation window. Candidate for downsizing or shutdown scheduling.",
        )

    if cpu_p95 is not None and cpu_p95 < 25 and (cpu_peak is None or cpu_peak < 50):
        return (
            "scale-down-candidate",
            "CPU p95 below 25 % with moderate peaks. Evaluate a smaller VM size for cost savings.",
        )

    if cpu_peak is not None and cpu_peak >= 85:
        return (
            "scale-up-review",
            "High peak CPU observed. Review whether the current VM size is adequate.",
        )

    return (
        "review",
        "VM is running with no clear red flag. Use metric trends for manual review.",
    )


def get_metric_series(
    monitor_client: MonitorManagementClient,
    resource_id: str,
    metric_name: str,
    timespan: str,
    aggregation: str,
    interval: str,
) -> list[tuple[str, float | None]]:
    try:
        result = monitor_client.metrics.list(
            resource_uri=resource_id,
            metricnames=metric_name,
            aggregation=aggregation,
            timespan=timespan,
            interval=interval,
        )
    except Exception as exc:
        print(f"    ⚠  Metric query failed ({metric_name}): {exc}")
        return []

    output = []
    property_name = aggregation.lower()
    for metric in result.value:
        for timeseries in metric.timeseries:
            for point in timeseries.data:
                timestamp = point.time_stamp.strftime("%Y-%m-%dT%H:%M:%SZ") if point.time_stamp else ""
                value = getattr(point, property_name, None)
                if value is None:
                    continue
                output.append((timestamp, float(value)))
    return output


def fetch_vm_metric_series(
    monitor_client: MonitorManagementClient,
    vm_id: str,
    timespan: str,
    interval: str,
) -> dict[str, list[tuple[str, float | None]]]:
    series_by_column = {}
    for metric in VM_METRICS:
        series_by_column[metric["column"]] = get_metric_series(
            monitor_client,
            vm_id,
            metric["azure_name"],
            timespan,
            metric["aggregation"],
            interval,
        )
    return series_by_column


def process_subscription(
    credential,
    sub_id: str,
    sub_name: str,
    tenant_id: str,
    timespan: str,
    interval: str,
) -> tuple[list[dict], list[dict]]:
    print(f"\n── Subscription: {sub_name or sub_id} ──")
    compute_client = ComputeManagementClient(credential, sub_id)
    monitor_client = MonitorManagementClient(credential, sub_id)
    summary_rows: list[dict] = []
    ts_rows: list[dict] = []
    nic_public_ip_cache: dict[str, list[str]] = {}

    try:
        arm_token = get_token(credential)
    except Exception as exc:
        print(f"  ⚠  Could not get ARM token for network/backup enrichment: {exc}")
        arm_token = ""

    backup_index = build_backup_index(arm_token, sub_id) if arm_token else {}

    try:
        vms = list(compute_client.virtual_machines.list_all())
    except Exception as exc:
        print(f"  ⚠  Could not list Virtual Machines: {exc}")
        return summary_rows, ts_rows

    if not vms:
        print("  (no Virtual Machines found)")
        return summary_rows, ts_rows

    print(f"  Found {len(vms)} Virtual Machine(s)")

    for vm in vms:
        vm_id = fmt(vm.id)
        resource_group = parse_resource_group(vm_id)
        vm_name = fmt(vm.name)

        # ── Hardware / OS profile ─────────────────────────────────────────────
        hardware = getattr(vm, "hardware_profile", None)
        vm_size = fmt(getattr(hardware, "vm_size", None))

        os_profile = getattr(vm, "os_profile", None)
        storage_profile = getattr(vm, "storage_profile", None)
        os_disk = getattr(storage_profile, "os_disk", None) if storage_profile else None
        data_disks = getattr(storage_profile, "data_disks", None) if storage_profile else []
        data_disks = data_disks or []

        os_type_raw = getattr(os_disk, "os_type", None) if os_disk else None
        os_type = fmt(os_type_raw) if os_type_raw else (
            "Windows" if getattr(os_profile, "windows_configuration", None) else
            "Linux" if getattr(os_profile, "linux_configuration", None) else ""
        )

        os_disk_sku_raw = getattr(os_disk, "managed_disk", None)
        os_disk_sku = fmt(getattr(os_disk_sku_raw, "storage_account_type", None)) if os_disk_sku_raw else ""
        os_disk_size_gb = getattr(os_disk, "disk_size_gb", None) if os_disk else None

        data_disk_count = len(data_disks)
        data_disk_total_size_gb = sum(
            (d.disk_size_gb or 0) for d in data_disks if d.disk_size_gb is not None
        ) or None

        # ── Network ───────────────────────────────────────────────────────────
        network_profile = getattr(vm, "network_profile", None)
        nic_refs = getattr(network_profile, "network_interfaces", None) or []
        nic_count = len(nic_refs)
        has_public_ip, public_ip_addresses = (
            get_vm_public_ip_info(arm_token, nic_refs, nic_public_ip_cache) if arm_token else (False, "")
        )

        # ── Availability ──────────────────────────────────────────────────────
        zones = getattr(vm, "zones", None) or []
        availability_zone = ", ".join(zones)

        avset = getattr(vm, "availability_set", None)
        availability_set = parse_resource_group(fmt(getattr(avset, "id", None))).split("/")[-1] if avset else ""
        # More precise: extract just the resource name from the id
        avset_id = fmt(getattr(avset, "id", None)) if avset else ""
        if avset_id and "/" in avset_id:
            availability_set = avset_id.split("/")[-1]

        ppg = getattr(vm, "proximity_placement_group", None)
        ppg_id = fmt(getattr(ppg, "id", None)) if ppg else ""
        proximity_placement_group = ppg_id.split("/")[-1] if ppg_id and "/" in ppg_id else ppg_id

        # ── Security ──────────────────────────────────────────────────────────
        additional_caps = getattr(vm, "additional_capabilities", None)
        ultra_ssd_enabled = getattr(additional_caps, "ultra_ssd_enabled", None) if additional_caps else None

        security_profile = getattr(vm, "security_profile", None)
        encryption_at_host = getattr(security_profile, "encryption_at_host", None) if security_profile else None
        uefi_settings = getattr(security_profile, "uefi_settings", None) if security_profile else None
        secure_boot_enabled = getattr(uefi_settings, "secure_boot_enabled", None) if uefi_settings else None
        vtpm_enabled = getattr(uefi_settings, "v_tpm_enabled", None) if uefi_settings else None

        # ── Licensing / priority ──────────────────────────────────────────────
        license_type = fmt(getattr(vm, "license_type", None))
        priority = fmt(getattr(vm, "priority", None))
        eviction_policy = fmt(getattr(vm, "eviction_policy", None))

        # ── Managed identity ──────────────────────────────────────────────────
        identity = getattr(vm, "identity", None)
        identity_type = fmt(getattr(identity, "type", None)) if identity else ""

        # ── Extensions ───────────────────────────────────────────────────────
        try:
            ext_list = list(compute_client.virtual_machine_extensions.list(resource_group, vm_name))
            extensions = ", ".join(sorted(fmt(e.name) for e in ext_list if e.name))
        except Exception:
            extensions = ""

        # ── Power state ───────────────────────────────────────────────────────
        print(
            f"\n  VM: {vm_name}  (RG: {resource_group})  Size: {vm_size or 'unknown'}  OS: {os_type or 'unknown'}",
            end="",
            flush=True,
        )
        power_state = get_power_state(compute_client, resource_group, vm_name)
        print(f"  State: {power_state or 'unknown'}")


        # Parse tags as dict for easier lookup
        raw_tags = getattr(vm, "tags", None)
        tags_dict = dict(raw_tags) if raw_tags else {}
        tags_string = tags_str(raw_tags)
        is_databricks = any(
            (str(k).lower() == "vendor" and str(v).lower() == "databricks")
            for k, v in tags_dict.items()
        )

        managed_by = "Azure Databricks" if is_databricks else ""

        vm_info = {
            "tenant_id": tenant_id or "",
            "subscription_id": sub_id,
            "subscription_name": sub_name or "",
            "resource_group": resource_group,
            "vm_name": vm_name,
            "vm_id": vm_id,
            "location": fmt(vm.location),
            "vm_size": vm_size,
            "os_type": os_type,
            "os_disk_sku": os_disk_sku,
            "os_disk_size_gb": os_disk_size_gb,
            "data_disk_count": data_disk_count,
            "data_disk_total_size_gb": data_disk_total_size_gb,
            "nic_count": nic_count,
            "has_public_ip": has_public_ip,
            "public_ip_addresses": public_ip_addresses,
            "availability_zone": availability_zone,
            "availability_set": availability_set,
            "proximity_placement_group": proximity_placement_group,
            "ultra_ssd_enabled": ultra_ssd_enabled,
            "encryption_at_host": encryption_at_host,
            "secure_boot_enabled": secure_boot_enabled,
            "vtpm_enabled": vtpm_enabled,
            "license_type": license_type,
            "priority": priority,
            "eviction_policy": eviction_policy,
            "identity_type": identity_type,
            "extensions": extensions,
            "power_state": power_state,
            "provisioning_state": fmt(getattr(vm, "provisioning_state", None)),
            "tags": tags_string,
            "managed_by": managed_by,
        }

        # Backup fields: if Databricks-managed, set to N/A
        if is_databricks:
            vm_info["backup_configured"] = "N/A"
            vm_info["backup_status"] = "N/A"
            vm_info["backup_last_run_utc"] = "N/A"
        else:
            vm_info["backup_configured"] = False
            vm_info["backup_status"] = "Not configured"
            vm_info["backup_last_run_utc"] = ""

        backup_info = backup_index.get(vm_id.lower())
        if backup_info and not is_databricks:
            vm_info["backup_configured"] = bool(backup_info.get("backup_configured"))
            vm_info["backup_status"] = fmt(backup_info.get("backup_status"))
            vm_info["backup_last_run_utc"] = fmt(backup_info.get("backup_last_run_utc"))

        backup_info = backup_index.get(vm_id.lower())
        if backup_info:
            vm_info["backup_configured"] = bool(backup_info.get("backup_configured"))
            vm_info["backup_status"] = fmt(backup_info.get("backup_status"))
            vm_info["backup_last_run_utc"] = fmt(backup_info.get("backup_last_run_utc"))

        # ── Metrics — skip for deallocated VMs ───────────────────────────────
        metric_series: dict = {}
        if power_state != "deallocated":
            print("    Fetching VM metrics ...", end=" ", flush=True)
            metric_series = fetch_vm_metric_series(monitor_client, vm_id, timespan, interval)
            ts_set = sorted(set(ts for series in metric_series.values() for ts, _ in series))
            print(f"{len(ts_set)} time points")
        else:
            print("    (deallocated — skipping metrics)")
            ts_set = []

        metric_summaries = {}
        for col_name, series in metric_series.items():
            values = [v for _, v in series if v is not None]
            metric_summaries[col_name] = summarize_metric_values(values, col_name)

        # Ensure all metric columns have an entry even if not fetched
        for m in VM_METRICS:
            if m["column"] not in metric_summaries:
                metric_summaries[m["column"]] = summarize_metric_values([], m["column"])

        ahb_active, ahb_description = describe_hybrid_benefit(vm_info, metric_summaries)
        cost_signal_category, cost_signal_reason = build_cost_signal(vm_info, metric_summaries, ahb_active)

        base_row = {
            **vm_info,
            "hybrid_benefit_active": ahb_active,
            "hybrid_benefit_description": ahb_description,
            "cost_signal_category": cost_signal_category,
            "cost_signal_reason": cost_signal_reason,
            "observed_sample_count": max((s["samples"] for s in metric_summaries.values()), default=0),
            "cpu_avg_pct_window": metric_summaries["cpu_percentage"]["avg"],
            "cpu_p95_pct_window": metric_summaries["cpu_percentage"]["p95"],
            "cpu_peak_pct_window": metric_summaries["cpu_percentage"]["peak"],
            "network_in_total_bytes_window": metric_summaries["network_in_bytes"]["total"],
            "network_out_total_bytes_window": metric_summaries["network_out_bytes"]["total"],
            "disk_read_total_bytes_window": metric_summaries["disk_read_bytes"]["total"],
            "disk_write_total_bytes_window": metric_summaries["disk_write_bytes"]["total"],
            "available_memory_avg_bytes_window": metric_summaries["available_memory_bytes"]["avg"],
            "available_memory_min_bytes_window": metric_summaries["available_memory_bytes"]["min"],
        }

        # ── Summary row ───────────────────────────────────────────────────────
        summary_rows.append({key: number_to_str(base_row.get(key)) for key in SUMMARY_COLUMNS})

        # ── Timeseries rows ───────────────────────────────────────────────────
        ts_map = {ts: {} for ts in ts_set}
        for col_name, series in metric_series.items():
            for ts, value in series:
                if ts in ts_map:
                    ts_map[ts][col_name] = value

        for ts in ts_set:
            values = ts_map[ts]
            ts_row = {
                "vm_id": vm_id,
                "timestamp": ts,
                "cpu_percentage": values.get("cpu_percentage"),
                "network_in_bytes": values.get("network_in_bytes"),
                "network_out_bytes": values.get("network_out_bytes"),
                "disk_read_bytes": values.get("disk_read_bytes"),
                "disk_write_bytes": values.get("disk_write_bytes"),
                "available_memory_bytes": values.get("available_memory_bytes"),
            }
            ts_rows.append({key: number_to_str(ts_row[key]) for key in TIMESERIES_COLUMNS})

    return summary_rows, ts_rows


def export(args):
    now = datetime.now(timezone.utc)
    fmt_iso = "%Y-%m-%dT%H:%M:%SZ"

    if args.date_from:
        start = parse_date(args.date_from)
        end = parse_date(args.date_to) if args.date_to else now
        if start >= end:
            print(f"ERROR: --from ({args.date_from}) must be before --to ({args.date_to or 'now'})")
            sys.exit(1)
        print(f"📅  Date range: {start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        lookback = parse_lookback(args.lookback or "PT168H")
        start = now - lookback
        end = now
        print(f"📅  Lookback: last {lookback}  ({start.strftime('%Y-%m-%d %H:%M')} → {end.strftime('%Y-%m-%d %H:%M')} UTC)")

    timespan = f"{start.strftime(fmt_iso)}/{end.strftime(fmt_iso)}"
    all_summary: list[dict] = []
    all_ts: list[dict] = []

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
                s, t = process_subscription(
                    credential, sub_id, sub_name, tenant_id, timespan, args.interval
                )
                all_summary.extend(s)
                all_ts.extend(t)
    elif args.subscription:
        credential = get_credential()
        s, t = process_subscription(
            credential, args.subscription, "", "", timespan, args.interval
        )
        all_summary.extend(s)
        all_ts.extend(t)
    else:
        credential = get_credential(None, args.sp_client_id, args.sp_client_secret, args.sp_certificate)
        subs = list_enabled_subscriptions(credential)
        print(f"Found {len(subs)} enabled subscription(s) via ARM SDK")
        for sub_id, sub_name, tenant_id in subs:
            s, t = process_subscription(
                credential, sub_id, sub_name, tenant_id, timespan, args.interval
            )
            all_summary.extend(s)
            all_ts.extend(t)

    os.makedirs(args.output_dir, exist_ok=True)
    date_range = f"{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}"

    if args.input:
        prefix = os.path.splitext(os.path.basename(args.input))[0].upper()
        summary_filename = f"{prefix}_virtual_machines_summary_{date_range}.csv"
        ts_filename = f"{prefix}_virtual_machines_timeseries_{date_range}.csv"
    else:
        summary_filename = f"virtual_machines_summary_{date_range}.csv"
        ts_filename = f"virtual_machines_timeseries_{date_range}.csv"

    summary_path = os.path.join(args.output_dir, summary_filename)
    ts_path = os.path.join(args.output_dir, ts_filename)
    summary_json_path = os.path.join(args.output_dir, summary_filename.replace(".csv", ".json"))
    ts_json_path = os.path.join(args.output_dir, ts_filename.replace(".csv", ".json"))

    if args.output_format in ("csv", "both"):
        with open(summary_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUMMARY_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(all_summary)

        with open(ts_path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TIMESERIES_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(all_ts)

    if args.output_format in ("json", "both"):
        with open(summary_json_path, "w", encoding="utf-8") as handle:
            json.dump(all_summary, handle, indent=2)

        with open(ts_json_path, "w", encoding="utf-8") as handle:
            json.dump(all_ts, handle, indent=2)

    print(f"\n{'═' * 70}")
    print(f"  ✅  Exported {len(all_summary)} VM(s), {len(all_ts)} timeseries row(s)")
    if args.output_format in ("csv", "both"):
        print(f"      → {summary_path}")
        print(f"      → {ts_path}")
    if args.output_format in ("json", "both"):
        print(f"      → {summary_json_path}")
        print(f"      → {ts_json_path}")
    print(f"{'═' * 70}\n")


def main():
    parser = argparse.ArgumentParser(
        description="Export Azure Virtual Machine inventory, power state, and rightsizing metrics.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
    %(prog)s -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.csv --lookback PT168H --interval PT1H --output-dir ./reports/CUST
    %(prog)s -s <subscription-id> --from 2026-01-01 --to 2026-03-31 --output-dir ./reports
        """,
    )

    parser.add_argument(
        "-i", "--input",
        default=None,
        help="Path to semicolon-delimited customer CSV (all rows processed)",
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
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Start date YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="End date YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS (defaults to now)",
    )
    parser.add_argument(
        "--lookback",
        default="PT168H",
        help="Lookback window when --from is omitted. Integer minutes or ISO-8601 duration (default: PT168H)",
    )
    parser.add_argument(
        "--interval",
        default="PT1H",
        help="Azure Monitor metric interval (default: PT1H)",
    )

    args = parser.parse_args()
    export(args)


if __name__ == "__main__":
    main()
