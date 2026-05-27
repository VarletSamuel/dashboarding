#!/usr/bin/env python3
"""
Azure Event Hubs – Namespace & Metric Exporter
===============================================
Exports all Event Hub namespaces with their event hubs, throughput unit
configuration, and real metric data from Azure Monitor into two CSV files:

  *_eventhub_summary_*.csv
      One row per (namespace × event hub). Contains all namespace metadata,
      TU configuration, cost advisory fields, and event hub inventory.
      No time-series data — suitable for advisory and inventory views.

  *_eventhub_timeseries_*.csv
      One row per (namespace, timestamp). Contains only the 5-minute metric
      values. Namespace metadata is NOT repeated — join on namespace_name.

Output: semicolon-delimited, BOM-prefixed CSV (Excel-compatible).

Prerequisites
-------------
    pip install azure-identity azure-mgmt-eventhub azure-mgmt-monitor

Authentication
--------------
    Same as get_daily_costs / get_workload_profiles: reads the customers.csv
    (-i), groups subscriptions by tenant, runs `az login --tenant` once per
    tenant (unless --skip-login is set), then iterates all matching
    subscriptions.

Usage
-----
    python get_eventhubnamespaces.py -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    python get_eventhubnamespaces.py -i ../customers/CUST.csv --lookback PT6H --output-dir ./reports/CUST
    python get_eventhubnamespaces.py -i ../customers/CUST.csv --from 2026-01-01 --to 2026-03-26 --output-dir ./reports/CUST
    python get_eventhubnamespaces.py -s <subscription-id> --output-dir ./reports
"""

import argparse
import csv
import json
import math
import os
import requests
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone


# ── dependency check ─────────────────────────────────────────────────────────

_REQUIRED = {
    "azure.identity":      "azure-identity",
    "azure.mgmt.eventhub": "azure-mgmt-eventhub",
    "azure.mgmt.monitor":  "azure-mgmt-monitor",
    "requests":           "requests",
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
        print(f"    -  {pkg}")
    print(f"\nPython executable : {sys.executable}")
    print(f"Python version    : {sys.version}")
    print(f"\nInstall into the same Python that runs this script:")
    print(f"\n    {sys.executable} -m pip install {' '.join(_missing)}\n")
    sys.exit(1)

from azure.identity import (
    AzureCliCredential,
    CertificateCredential,
    ChainedTokenCredential,
    ClientSecretCredential,
    DefaultAzureCredential,
)
from azure.mgmt.eventhub import EventHubManagementClient
from azure.mgmt.monitor import MonitorManagementClient


# ── CSV columns ───────────────────────────────────────────────────────────────

# Summary file: one row per (namespace x eventhub). No time-series data.
SUMMARY_COLUMNS = [
    "tenant_id",
    "subscription_id",
    "subscription_name",
    "resource_group",
    "namespace_name",
    "namespace_id",
    "location",
    "sku_tier",
    "sku_capacity",
    "is_auto_inflate_enabled",
    "max_throughput_units",
    "namespace_required_tu_avg",
    "namespace_required_tu_p95",
    "namespace_required_tu_peak",
    "namespace_recommended_tu_p95",
    "namespace_tu_utilization_avg_pct",
    "namespace_tu_utilization_p95_pct",
    "namespace_tu_utilization_peak_pct",
    "namespace_throttled_points",
    "namespace_throttled_total",
    "namespace_needs_more_than_capacity",
    "namespace_possible_tu_savings",
    "namespace_cost_risk",
    "namespace_recommendation",
    "namespace_recommendation_reason",
    "eventhub_name",
    "partition_count",
    "message_retention_days",
    "eventhub_status",
]

# Timeseries file: one row per (namespace, timestamp). No repeated metadata.
TIMESERIES_COLUMNS = [
    "namespace_name",
    "timestamp",
    "ingress_messages_per_sec",
    "egress_messages_per_sec",
    "ingress_mb_per_sec",
    "egress_mb_per_sec",
    "estimated_required_tu",
    "incoming_messages",
    "outgoing_messages",
    "incoming_bytes",
    "outgoing_bytes",
    "throttled_requests",
]

# Azure Monitor metric names at namespace scope
NAMESPACE_METRICS = [
    "IncomingMessages",
    "OutgoingMessages",
    "IncomingBytes",
    "OutgoingBytes",
    "ThrottledRequests",
]


METRIC_INTERVAL_SECONDS = 5 * 60
INGRESS_MBPS_PER_TU = 1.0
EGRESS_MBPS_PER_TU = 2.0
INGRESS_MSGS_PER_SEC_PER_TU = 1000.0
EGRESS_MSGS_PER_SEC_PER_TU = 4096.0


# ── helpers ───────────────────────────────────────────────────────────────────

def parse_lookback(value: str) -> timedelta:
    """Accept an integer (minutes) or ISO-8601 duration like PT6H."""
    try:
        return timedelta(minutes=int(value))
    except ValueError:
        pass
    import re
    m = re.match(r"PT(?:(\d+)D)?(?:(\d+)H)?(?:(\d+)M)?", value, re.IGNORECASE)
    if m:
        return timedelta(
            days=int(m.group(1) or 0),
            hours=int(m.group(2) or 0),
            minutes=int(m.group(3) or 0),
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
    """
    Read the customer JSON file.
    Returns { tenant_id: [(subscription_id, subscription_name), ...] }
    """
    if not os.path.exists(json_path):
        print(f"ERROR: Customer file not found: {json_path}")
        sys.exit(1)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

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


def required_tu_from_point(
    incoming_messages: float | None,
    outgoing_messages: float | None,
    incoming_bytes: float | None,
    outgoing_bytes: float | None,
    interval_seconds: int = METRIC_INTERVAL_SECONDS,
) -> dict[str, float]:
    in_msg = incoming_messages or 0.0
    out_msg = outgoing_messages or 0.0
    in_bytes = incoming_bytes or 0.0
    out_bytes = outgoing_bytes or 0.0

    ingress_messages_per_sec = in_msg / interval_seconds
    egress_messages_per_sec = out_msg / interval_seconds
    ingress_mb_per_sec = (in_bytes / (1024.0 * 1024.0)) / interval_seconds
    egress_mb_per_sec = (out_bytes / (1024.0 * 1024.0)) / interval_seconds

    required_tu = max(
        ingress_messages_per_sec / INGRESS_MSGS_PER_SEC_PER_TU,
        egress_messages_per_sec / EGRESS_MSGS_PER_SEC_PER_TU,
        ingress_mb_per_sec / INGRESS_MBPS_PER_TU,
        egress_mb_per_sec / EGRESS_MBPS_PER_TU,
    )

    return {
        "ingress_messages_per_sec": ingress_messages_per_sec,
        "egress_messages_per_sec": egress_messages_per_sec,
        "ingress_mb_per_sec": ingress_mb_per_sec,
        "egress_mb_per_sec": egress_mb_per_sec,
        "estimated_required_tu": required_tu,
    }


def classify_namespace_cost(
    sku_capacity: float | None,
    max_throughput_units: float | None,
    auto_inflate: bool,
    required_tu_avg: float | None,
    required_tu_p95: float | None,
    required_tu_peak: float | None,
    throttled_total: float,
    throttled_points: int,
) -> tuple[str, str, str, int]:
    cap = float(sku_capacity or 0.0)
    max_tu = float(max_throughput_units) if max_throughput_units is not None else None
    req_p95 = float(required_tu_p95 or 0.0)
    req_peak = float(required_tu_peak or 0.0)

    recommended_tu_p95 = max(1, int(math.ceil(req_p95))) if cap > 0 else 0
    possible_savings = int(max(0, cap - recommended_tu_p95)) if cap > 0 else 0

    if req_peak <= 0.05 and throttled_total <= 0:
        return (
            "high",
            "idle-namespace",
            "Namespace has near-zero throughput. Consider decommissioning or consolidating to reduce TU spend.",
            possible_savings,
        )

    if throttled_total > 0 or throttled_points > 0 or (cap > 0 and req_peak > cap):
        if auto_inflate:
            if max_tu is not None and req_peak > max_tu:
                return (
                    "high",
                    "increase-max-tu",
                    "Traffic peaks exceed configured max TU during auto-inflate. Increase max TU to avoid throttling.",
                    possible_savings,
                )
            return (
                "medium",
                "watch-autoinflate",
                "Namespace is nearing or exceeding current TU. Keep auto-inflate enabled and review minimum TU baseline.",
                possible_savings,
            )
        return (
            "high",
            "enable-autoinflate-or-increase-tu",
            "Observed throttling or throughput above provisioned TU. Increase TU and/or enable auto-inflate.",
            possible_savings,
        )

    if cap > 1 and req_p95 <= cap * 0.35:
        return (
            "medium",
            "downsize-tu",
            "P95 required TU is well below provisioned TU. Consider reducing baseline TU capacity.",
            possible_savings,
        )

    if cap > 0 and req_p95 >= cap * 0.85:
        return (
            "medium",
            "near-capacity",
            "P95 throughput is close to provisioned TU. Monitor growth and keep headroom for bursts.",
            possible_savings,
        )

    return (
        "low",
        "right-sized",
        "Provisioned TU aligns reasonably with observed throughput profile.",
        possible_savings,
    )


# ── metric helpers ─────────────────────────────────────────────────────────────

def get_metric_series(
    monitor_client: MonitorManagementClient,
    resource_id: str,
    metric_name: str,
    timespan: str,
    interval: str = "PT5M",
    aggregation: str = "Total",
) -> list[tuple[str, float | None]]:
    """
    Return a list of (timestamp_str, value) tuples for a single metric.
    Uses 'Total' aggregation (sum over interval) for message/byte counters,
    which is more meaningful than Average for Event Hubs.
    """
    try:
        result = monitor_client.metrics.list(
            resource_uri=resource_id,
            metricnames=metric_name,
            aggregation=aggregation,
            timespan=timespan,
            interval=interval,
        )
        series = []
        for m in result.value:
            for ts in m.timeseries:
                for dp in ts.data:
                    ts_str = (
                        dp.time_stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
                        if dp.time_stamp else ""
                    )
                    val = getattr(dp, aggregation.lower(), None)
                    series.append((ts_str, round(val, 2) if val is not None else None))
        return series
    except Exception as exc:
        print(f"  WARNING: Metric query failed ({metric_name}): {exc}")
        return []


def fetch_namespace_metric_series(
    monitor_client: MonitorManagementClient,
    namespace_id: str,
    timespan: str,
) -> dict[str, list[tuple[str, float | None]]]:
    """
    Fetch all tracked metrics for a namespace; returns a dict keyed by metric name.
    ThrottledRequests uses 'Total'; all others use 'Total' as well since they
    are cumulative counters that make most sense summed per interval.
    """
    series_by_metric = {}
    for metric_name in NAMESPACE_METRICS:
        series_by_metric[metric_name] = get_metric_series(
            monitor_client, namespace_id, metric_name, timespan,
            interval="PT5M", aggregation="Total",
        )
    return series_by_metric


# ── process a single subscription ─────────────────────────────────────────────

def process_subscription(
    credential,
    sub_id: str,
    sub_name: str,
    tenant_id: str,
    timespan: str,
) -> tuple[list[dict], list[dict]]:
    """Process one subscription and return (summary_rows, timeseries_rows)."""
    print(f"\n── Subscription: {sub_name or sub_id} ──")
    eh_client = EventHubManagementClient(credential, sub_id)
    monitor_client = MonitorManagementClient(credential, sub_id)
    summary_rows: list[dict] = []
    ts_rows: list[dict] = []

    # List all Event Hub namespaces in the subscription
    try:
        namespaces = list(eh_client.namespaces.list())
    except Exception as exc:
        print(f"  WARNING: Could not list Event Hub namespaces: {exc}")
        return summary_rows, ts_rows

    if not namespaces:
        print("  (no Event Hub namespaces found)")
        return summary_rows, ts_rows

    print(f"  Found {len(namespaces)} namespace(s)")

    for ns in namespaces:
        ns_name = ns.name
        ns_id   = ns.id
        ns_rg   = ns_id.split("/resourceGroups/")[1].split("/")[0]
        location = ns.location or ""

        # Throughput unit config (Standard tier only; Premium uses PUs)
        sku_tier     = ns.sku.name if ns.sku else ""
        sku_capacity = ns.sku.capacity if ns.sku else None        # current TU (or PU)
        auto_inflate = getattr(ns, "is_auto_inflate_enabled", False) or False
        max_tu       = getattr(ns, "maximum_throughput_units", None)

        print(
            f"\n  Namespace: {ns_name}  (RG: {ns_rg})  "
            f"SKU: {sku_tier} / {sku_capacity} TU  "
            f"AutoInflate: {auto_inflate}"
            + (f"  MaxTU: {max_tu}" if max_tu else "")
        )

        # Fetch metric time series at namespace scope
        print(f"    Fetching metrics ...", end=" ", flush=True)
        metric_series = fetch_namespace_metric_series(monitor_client, ns_id, timespan)
        ts_set = sorted(
            set(ts for series in metric_series.values() for ts, _ in series)
        )
        print(f"{len(ts_set)} time points")

        # Build per-timestamp metric lookup
        ts_map: dict[str, dict[str, float | None]] = {ts: {} for ts in ts_set}
        for metric_name, series in metric_series.items():
            for ts, val in series:
                if ts in ts_map:
                    ts_map[ts][metric_name] = val

        required_tu_points: list[float] = []
        throttled_total = 0.0
        throttled_points = 0
        for ts in ts_set:
            mv = ts_map[ts]
            point = required_tu_from_point(
                mv.get("IncomingMessages"),
                mv.get("OutgoingMessages"),
                mv.get("IncomingBytes"),
                mv.get("OutgoingBytes"),
                interval_seconds=METRIC_INTERVAL_SECONDS,
            )
            mv["ingress_messages_per_sec"] = point["ingress_messages_per_sec"]
            mv["egress_messages_per_sec"] = point["egress_messages_per_sec"]
            mv["ingress_mb_per_sec"] = point["ingress_mb_per_sec"]
            mv["egress_mb_per_sec"] = point["egress_mb_per_sec"]
            mv["estimated_required_tu"] = point["estimated_required_tu"]
            required_tu_points.append(point["estimated_required_tu"])

            throttled = mv.get("ThrottledRequests") or 0.0
            throttled_total += throttled
            if throttled > 0:
                throttled_points += 1

        required_tu_avg = (sum(required_tu_points) / len(required_tu_points)) if required_tu_points else None
        required_tu_p95 = percentile(required_tu_points, 95)
        required_tu_peak = max(required_tu_points) if required_tu_points else None
        tu_util_avg = (required_tu_avg / sku_capacity * 100.0) if sku_capacity and required_tu_avg is not None else None
        tu_util_p95 = (required_tu_p95 / sku_capacity * 100.0) if sku_capacity and required_tu_p95 is not None else None
        tu_util_peak = (required_tu_peak / sku_capacity * 100.0) if sku_capacity and required_tu_peak is not None else None
        rec_risk, recommendation, recommendation_reason, possible_savings = classify_namespace_cost(
            sku_capacity,
            max_tu,
            auto_inflate,
            required_tu_avg,
            required_tu_p95,
            required_tu_peak,
            throttled_total,
            throttled_points,
        )
        rec_tu_p95 = max(1, int(math.ceil(required_tu_p95))) if required_tu_p95 is not None and (sku_capacity or 0) > 0 else ""
        needs_more_than_capacity = bool((required_tu_peak or 0) > (sku_capacity or 0)) if sku_capacity is not None else False

        if required_tu_p95 is not None and required_tu_peak is not None:
            print(
                f"    Cost signal: {recommendation} | risk={rec_risk} | "
                f"reqTU p95={required_tu_p95:.2f} peak={required_tu_peak:.2f}"
            )
        else:
            print(f"    Cost signal: {recommendation} | risk={rec_risk}")

        # Shared namespace fields
        ns_base = {
            "tenant_id":              tenant_id or "",
            "subscription_id":        sub_id,
            "subscription_name":      sub_name or "",
            "resource_group":         ns_rg,
            "namespace_name":         ns_name,
            "namespace_id":           ns_id,
            "location":               location,
            "sku_tier":               sku_tier,
            "sku_capacity":           sku_capacity if sku_capacity is not None else "",
            "is_auto_inflate_enabled": str(auto_inflate),
            "max_throughput_units":   max_tu if max_tu is not None else "",
            "namespace_required_tu_avg": required_tu_avg if required_tu_avg is not None else "",
            "namespace_required_tu_p95": required_tu_p95 if required_tu_p95 is not None else "",
            "namespace_required_tu_peak": required_tu_peak if required_tu_peak is not None else "",
            "namespace_recommended_tu_p95": rec_tu_p95,
            "namespace_tu_utilization_avg_pct": tu_util_avg if tu_util_avg is not None else "",
            "namespace_tu_utilization_p95_pct": tu_util_p95 if tu_util_p95 is not None else "",
            "namespace_tu_utilization_peak_pct": tu_util_peak if tu_util_peak is not None else "",
            "namespace_throttled_points": throttled_points,
            "namespace_throttled_total": throttled_total,
            "namespace_needs_more_than_capacity": str(needs_more_than_capacity),
            "namespace_possible_tu_savings": possible_savings,
            "namespace_cost_risk": rec_risk,
            "namespace_recommendation": recommendation,
            "namespace_recommendation_reason": recommendation_reason,
        }

        def fmt(v) -> str:
            return str(v) if v is not None else ""

        # ── Emit timeseries rows: one per (namespace, timestamp) ──────────────
        seen_ts: set[str] = set()
        for ts in ts_set:
            if ts in seen_ts:
                continue
            seen_ts.add(ts)
            mv = ts_map[ts]
            ts_rows.append({
                "namespace_name":            ns_name,
                "timestamp":                 ts,
                "ingress_messages_per_sec":  fmt(mv.get("ingress_messages_per_sec")),
                "egress_messages_per_sec":   fmt(mv.get("egress_messages_per_sec")),
                "ingress_mb_per_sec":        fmt(mv.get("ingress_mb_per_sec")),
                "egress_mb_per_sec":         fmt(mv.get("egress_mb_per_sec")),
                "estimated_required_tu":     fmt(mv.get("estimated_required_tu")),
                "incoming_messages":         fmt(mv.get("IncomingMessages")),
                "outgoing_messages":         fmt(mv.get("OutgoingMessages")),
                "incoming_bytes":            fmt(mv.get("IncomingBytes")),
                "outgoing_bytes":            fmt(mv.get("OutgoingBytes")),
                "throttled_requests":        fmt(mv.get("ThrottledRequests")),
            })

        # ── List Event Hubs in this namespace ─────────────────────────────────
        try:
            event_hubs = list(eh_client.event_hubs.list_by_namespace(ns_rg, ns_name))
        except Exception as exc:
            print(f"    WARNING: Could not list event hubs in {ns_name}: {exc}")
            event_hubs = []

        print(f"    Event Hubs: {len(event_hubs)}")

        if not event_hubs:
            # Emit one summary row for the namespace with no event hub detail
            summary_rows.append({
                **ns_base,
                "eventhub_name":          "",
                "partition_count":         "",
                "message_retention_days":  "",
                "eventhub_status":         "",
            })
            continue

        # ── Emit one summary row per event hub ────────────────────────────────
        for eh in event_hubs:
            eh_name    = eh.name
            partitions = getattr(eh, "partition_count", None)
            retention  = getattr(eh, "message_retention_in_days", None)
            status     = getattr(eh, "status", None) or ""
            status_str = status.value if hasattr(status, "value") else str(status)

            print(f"      ↳ {eh_name}  ({partitions} partitions, retention {retention}d)")

            summary_rows.append({
                **ns_base,
                "eventhub_name":          eh_name,
                "partition_count":         fmt(partitions),
                "message_retention_days":  fmt(retention),
                "eventhub_status":         status_str,
            })

    return summary_rows, ts_rows


# ── main export ───────────────────────────────────────────────────────────────

def export(args):
    now = datetime.now(timezone.utc)
    fmt_iso = "%Y-%m-%dT%H:%M:%SZ"

    # Determine timespan
    if args.date_from:
        start = parse_date(args.date_from)
        end   = parse_date(args.date_to) if args.date_to else now
        if start >= end:
            print(f"ERROR: --from ({args.date_from}) must be before --to ({args.date_to or 'now'})")
            sys.exit(1)
        print(f"Date range: {start.strftime('%Y-%m-%d %H:%M')} -> {end.strftime('%Y-%m-%d %H:%M')} UTC")
    else:
        lookback = parse_lookback(args.lookback or "60")
        start    = now - lookback
        end      = now
        print(f"Lookback: last {lookback}  ({start.strftime('%Y-%m-%d %H:%M')} -> {end.strftime('%Y-%m-%d %H:%M')} UTC)")

    timespan = f"{start.strftime(fmt_iso)}/{end.strftime(fmt_iso)}"

    all_summary: list[dict] = []
    all_ts: list[dict] = []

    if args.input:
        # ── CSV batch mode ───────────────────────────────────────
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
                s_rows, t_rows = process_subscription(credential, sub_id, sub_name, tenant_id, timespan)
                all_summary.extend(s_rows)
                all_ts.extend(t_rows)

    elif args.subscription:
        # ── Single subscription mode ─────────────────────────────
        credential = get_credential()
        s_rows, t_rows = process_subscription(credential, args.subscription, "", "", timespan)
        all_summary.extend(s_rows)
        all_ts.extend(t_rows)

    else:
        # ── All accessible subscriptions ─────────────────────────
        credential = get_credential(None, args.sp_client_id, args.sp_client_secret, args.sp_certificate)
        subs = list_enabled_subscriptions(credential)
        print(f"Found {len(subs)} enabled subscription(s) via ARM SDK")
        for sub_id, sub_name, tenant_id in subs:
            s_rows, t_rows = process_subscription(credential, sub_id, sub_name, tenant_id, timespan)
            all_summary.extend(s_rows)
            all_ts.extend(t_rows)

    # ── Write CSVs ────────────────────────────────────────────────
    os.makedirs(args.output_dir, exist_ok=True)

    if args.input:
        customer = os.path.splitext(os.path.basename(args.input))[0].upper()
        prefix = customer
    else:
        prefix = "eventhub"

    date_range = f"{start.strftime('%Y-%m-%d')}_{end.strftime('%Y-%m-%d')}"

    summary_path = os.path.join(args.output_dir, f"{prefix}_eventhub_summary_{date_range}.csv")
    ts_path      = os.path.join(args.output_dir, f"{prefix}_eventhub_timeseries_{date_range}.csv")
    summary_json_path = os.path.join(args.output_dir, f"{prefix}_eventhub_summary_{date_range}.json")
    ts_json_path = os.path.join(args.output_dir, f"{prefix}_eventhub_timeseries_{date_range}.json")

    if args.output_format in ("csv", "both"):
        with open(summary_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(all_summary)

        with open(ts_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TIMESERIES_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(all_ts)

    if args.output_format in ("json", "both"):
        with open(summary_json_path, "w", encoding="utf-8") as f:
            json.dump(all_summary, f, indent=2)

        with open(ts_json_path, "w", encoding="utf-8") as f:
            json.dump(all_ts, f, indent=2)

    # ── Summary ───────────────────────────────────────────────────
    unique_namespaces = len(set(r.get("namespace_name", "") for r in all_summary if r.get("namespace_name")))
    unique_eventhubs  = len(set(
        (r.get("namespace_name", ""), r.get("eventhub_name", ""))
        for r in all_summary if r.get("eventhub_name")
    ))

    print(f"\n{'=' * 70}")
    print(f"  Exported {unique_namespaces} namespace(s), {unique_eventhubs} event hub(s)")
    if args.output_format in ("csv", "both"):
        print(f"      Summary         ({len(all_summary)} rows) -> {summary_path}")
        print(f"      Timeseries      ({len(all_ts)} rows) -> {ts_path}")
    if args.output_format in ("json", "both"):
        print(f"      Summary JSON    ({len(all_summary)} rows) -> {summary_json_path}")
        print(f"      Timeseries JSON ({len(all_ts)} rows) -> {ts_json_path}")
    print(f"{'=' * 70}\n")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Export Azure Event Hub namespace and metric data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
    %(prog)s -i ../customers/CUST.csv --skip-login --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.csv --lookback PT6H --output-dir ./reports/CUST
    %(prog)s -i ../customers/CUST.csv --from 2026-01-01 --to 2026-03-26 --output-dir ./reports/CUST
  %(prog)s -s <subscription-id> --from 2026-03-01 --output-dir ./reports
        """,
    )

    parser.add_argument(
        "-i", "--input",
                default=None,
                help="Path to semicolon-delimited customer CSV (all rows processed)",
    )
    parser.add_argument(
        "-s", "--subscription",
        help="Single subscription ID (alternative to -c mode)",
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
        "--lookback", "-l",
        default=None,
        help="Metrics lookback window: minutes (int) or ISO-8601 duration "
             "like PT6H, PT30M (default: 60 minutes if --from/--to not set)",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Start date/time for metrics (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). "
             "Use with --to for an absolute date range.",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="End date/time for metrics (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS). "
             "Defaults to now when --from is provided without --to.",
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

    if not args.input and not args.subscription:
        print("ℹ  No -i or -s given — will scan all subscriptions in the default credential scope.")
    if args.date_from and args.lookback:
        parser.error("--from/--to and --lookback are mutually exclusive. Use one or the other.")
    if args.date_to and not args.date_from:
        parser.error("--to requires --from")
    export(args)


if __name__ == "__main__":
    main()
