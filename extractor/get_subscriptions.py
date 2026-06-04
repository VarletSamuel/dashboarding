#!/usr/bin/env python3
"""
Fetch all subscriptions visible in a tenant and include:
  - subscription status (state)
  - management group (direct parent, when available)
	- overall security score (Defender for Cloud secure score percentage)

Output:
  1 CSV file per run:
	 subscriptions_<tenant>_<yyyymmdd>.csv

Requirements:
  pip install azure-identity requests

Usage examples:
  python get_subscriptions.py
  python get_subscriptions.py --tenant-id <tenant-guid>
  python get_subscriptions.py --tenant-id <tenant-guid> --json
  python get_subscriptions.py --output-dir ../reports
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
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
# ARM helpers
# ---------------------------------------------------------------------------
def arm_get_json(token: str, url: str) -> dict:
	headers = {"Authorization": f"Bearer {token}"}

	while True:
		response = requests.get(url, headers=headers, timeout=120)
		if response.status_code == 429:
			retry_after = int(response.headers.get("Retry-After", "30"))
			print(f"Throttled on ARM GET, retrying in {retry_after}s...", flush=True)
			time.sleep(retry_after)
			continue
		response.raise_for_status()
		return response.json()


def arm_get_all(token: str, initial_url: str) -> list[dict]:
	results = []
	next_url = initial_url

	while next_url:
		data = arm_get_json(token, next_url)
		results.extend(data.get("value", []))
		next_url = data.get("nextLink")

	return results


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------
def fetch_subscriptions(token: str) -> list[dict]:
	url = "https://management.azure.com/subscriptions?api-version=2022-12-01"
	raw = arm_get_all(token, url)

	subscriptions = []
	for item in raw:
		subscriptions.append(
			{
				"subscription_id": (item.get("subscriptionId") or "").strip(),
				"subscription_name": (item.get("displayName") or "").strip(),
				"subscription_state": (item.get("state") or "").strip(),
				"tenant_id": (item.get("tenantId") or "").strip(),
			}
		)

	return subscriptions


# ---------------------------------------------------------------------------
# Management Group resolution
# ---------------------------------------------------------------------------
def extract_group_name_from_id(resource_id: str) -> str:
	if not resource_id:
		return ""
	match = re.search(r"/providers/Microsoft\.Management/managementGroups/([^/]+)", resource_id)
	return match.group(1) if match else ""


def fetch_management_group_mapping(token: str) -> dict[str, str]:
	"""
	Returns a mapping:
	  {subscription_id -> direct_parent_management_group_display_name}

	This uses the management group subscriptions endpoint for each management group.
	"""
	mg_mapping: dict[str, str] = {}

	mg_list_url = (
		"https://management.azure.com/providers/Microsoft.Management/managementGroups"
		"?api-version=2023-04-01"
	)
	management_groups = arm_get_all(token, mg_list_url)
	mg_display_names: dict[str, str] = {}

	for mg in management_groups:
		mg_name = (mg.get("name") or "").strip()
		if not mg_name:
			continue
		display_name = (mg.get("properties", {}).get("displayName") or "").strip()
		mg_display_names[mg_name] = display_name or mg_name

	for mg in management_groups:
		mg_name = (mg.get("name") or "").strip()
		if not mg_name:
			continue

		subs_url = (
			"https://management.azure.com/providers/Microsoft.Management/managementGroups/"
			f"{mg_name}/subscriptions?api-version=2020-05-01"
		)

		try:
			mg_subscriptions = arm_get_all(token, subs_url)
		except requests.HTTPError:
			# Continue if caller cannot read a particular management group.
			continue

		for sub in mg_subscriptions:
			sub_id = (sub.get("name") or "").strip()
			if not sub_id:
				continue

			parent_id = ""
			details = sub.get("properties", {}).get("details", {})
			if isinstance(details, dict):
				parent_id = details.get("parent", {}).get("id", "")

			parent_mg_name = extract_group_name_from_id(parent_id) or mg_name
			mg_mapping[sub_id] = mg_display_names.get(parent_mg_name, parent_mg_name)

	return mg_mapping


# ---------------------------------------------------------------------------
# Security score resolution
# ---------------------------------------------------------------------------
def _to_float(value) -> float | None:
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def _extract_secure_score_percent(payload: dict) -> float | None:
	"""
	Extract secure score percentage (0-100) from Microsoft.Security secureScores payload.
	"""
	items = payload.get("value", [])
	if not items:
		return None

	entry = next((i for i in items if (i.get("name") or "").lower() == "ascscore"), items[0])
	props = entry.get("properties", {})
	if not isinstance(props, dict):
		return None

	score_obj = props.get("score")
	if isinstance(score_obj, dict):
		percentage = _to_float(score_obj.get("percentage"))
		if percentage is not None:
			return percentage

		current = _to_float(score_obj.get("current"))
		maximum = _to_float(score_obj.get("max"))
		if maximum is None:
			maximum = _to_float(score_obj.get("maximum"))
		if current is not None and maximum and maximum > 0:
			return (current / maximum) * 100.0
		if current is not None and 0.0 <= current <= 1.0:
			return current * 100.0

	percentage = _to_float(props.get("percentage"))
	if percentage is not None:
		return percentage

	current = _to_float(props.get("current"))
	maximum = _to_float(props.get("max"))
	if maximum is None:
		maximum = _to_float(props.get("maximum"))
	if current is not None and maximum and maximum > 0:
		return (current / maximum) * 100.0

	return None


def fetch_security_score_mapping(token: str, subscription_ids: list[str]) -> dict[str, str]:
	"""
	Returns a mapping:
	  {subscription_id -> secure_score_percent_as_string}

	Missing access/provider registration issues are tolerated and returned as empty values.
	"""
	scores: dict[str, str] = {}

	for sub_id in subscription_ids:
		if not sub_id:
			continue
		url = (
			f"https://management.azure.com/subscriptions/{sub_id}"
			"/providers/Microsoft.Security/secureScores?api-version=2020-01-01"
		)
		try:
			payload = arm_get_json(token, url)
		except requests.HTTPError:
			scores[sub_id] = ""
			continue

		score = _extract_secure_score_percent(payload)
		if score is None:
			scores[sub_id] = ""
			continue
		# Normalize to score on 100 if API returns a fractional value (0.0-1.0).
		if 0.0 <= score <= 1.0:
			score = score * 100.0
		scores[sub_id] = f"{score:.2f}"

	return scores


def fetch_resource_count_mapping(token: str, subscription_ids: list[str]) -> dict[str, int]:
	"""Return total Azure resource count per subscription.

	Uses the ARM resources list endpoint and follows paging links.
	"""
	counts: dict[str, int] = {}
	headers = {"Authorization": f"Bearer {token}"}

	for sub_id in subscription_ids:
		if not sub_id:
			continue
		url = (
			f"https://management.azure.com/subscriptions/{sub_id}/resources"
			"?api-version=2021-04-01"
		)
		total = 0
		try:
			while url:
				response = requests.get(url, headers=headers, timeout=120)
				if response.status_code == 429:
					retry_after = int(response.headers.get("Retry-After", "30"))
					print(f"Throttled on resources list for {sub_id}, retrying in {retry_after}s...", flush=True)
					time.sleep(retry_after)
					continue
				response.raise_for_status()
				payload = response.json()
				total += len(payload.get("value", []))
				url = payload.get("nextLink")
		except requests.HTTPError:
			counts[sub_id] = 0
			continue

		counts[sub_id] = total

	return counts


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_csv(path: Path, records: list[dict], fieldnames: list[str]):
	with open(path, "w", newline="", encoding="utf-8") as handle:
		writer = csv.DictWriter(handle, fieldnames=fieldnames, delimiter=";")
		writer.writeheader()
		writer.writerows(records)


def write_json(path: Path, payload: dict):
	with open(path, "w", encoding="utf-8") as handle:
		json.dump(payload, handle, indent=2)


def load_customer_azure_entries(input_path: str | None) -> tuple[dict[str, dict], list[dict]]:
	"""Load customer azure entries keyed by subscription_id.

	Returns:
	  - map: {subscription_id -> entry}
	  - ordered list of raw azure entries (for preserving non-visible subscriptions)
	"""
	if not input_path:
		return {}, []

	path = Path(input_path)
	if not path.exists():
		print(f"WARN: customer input file not found: {input_path}")
		return {}, []

	try:
		payload = json.loads(path.read_text(encoding="utf-8"))
	except Exception as exc:
		print(f"WARN: could not parse customer input JSON ({input_path}): {exc}")
		return {}, []

	entries = []
	entry_map: dict[str, dict] = {}
	for item in payload.get("azure", []):
		if not isinstance(item, dict):
			continue
		sub_id = (item.get("subscription_id") or "").strip()
		if not sub_id:
			continue
		entries.append(item)
		entry_map[sub_id] = item

	return entry_map, entries


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
	parser = argparse.ArgumentParser(
		description="Fetch all subscriptions for a tenant with management group and status."
	)
	parser.add_argument(
		"--tenant-id",
		default=os.environ.get("AZURE_TENANT_ID"),
		help="Tenant ID to target. Defaults to AZURE_TENANT_ID when available.",
	)
	parser.add_argument(
		"--output-dir",
		default=".",
		help="Directory for output files. Default: current directory.",
	)
	parser.add_argument(
		"-i",
		"--input",
		default=None,
		help="Optional customer JSON path to merge customer subscription status values.",
	)
	parser.add_argument(
		"--output-format",
		choices=("csv", "json", "both"),
		default="both",
		help="File output format: csv, json, or both (default: both).",
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
		help="Client secret for service principal login. Falls back to AZURE_SP_CLIENT_SECRET.",
	)
	parser.add_argument(
		"--sp-certificate",
		default=None,
		metavar="CERT_PATH",
		help="Path to PEM certificate for service principal auth (alternative to --sp-client-secret).",
	)
	parser.add_argument(
		"--json",
		action="store_true",
		help="Also print JSON to stdout.",
	)
	args = parser.parse_args()

	try:
		credential = get_credential(
			args.tenant_id,
			args.sp_client_id,
			args.sp_client_secret,
			args.sp_certificate,
		)
		token = get_token(credential)
	except Exception as exc:
		print(f"ERROR: failed to authenticate: {exc}", file=sys.stderr)
		sys.exit(1)

	print("Fetching subscriptions...")
	subscriptions = fetch_subscriptions(token)
	customer_map, customer_entries = load_customer_azure_entries(args.input)

	print("Resolving management groups...")
	mg_mapping = fetch_management_group_mapping(token)

	print("Resolving overall security scores...")
	security_score_mapping = fetch_security_score_mapping(
		token,
		[sub.get("subscription_id", "") for sub in subscriptions],
	)

	print("Resolving resource counts...")
	resource_count_mapping = fetch_resource_count_mapping(
		token,
		[sub.get("subscription_id", "") for sub in subscriptions],
	)

	snapshot_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
	rows = []
	seen_sub_ids: set[str] = set()
	for sub in subscriptions:
		sub_id = sub["subscription_id"]
		seen_sub_ids.add(sub_id)
		customer_entry = customer_map.get(sub_id, {})
		customer_status = str(customer_entry.get("status") or "").strip()
		rows.append(
			{
				"tenant_id": sub["tenant_id"] or (args.tenant_id or ""),
				"subscription_id": sub_id,
				"subscription_name": sub["subscription_name"] or (customer_entry.get("subscription_name") or ""),
				"subscription_status": sub["subscription_state"],
				"customer_status": customer_status,
				"management_group": mg_mapping.get(sub_id, ""),
				"overall_security_score": security_score_mapping.get(sub_id, ""),
				"resource_count": resource_count_mapping.get(sub_id, 0),
				"snapshot_of": snapshot_utc,
				"azure_portal_url": f"https://portal.azure.com/#@/resource/subscriptions/{sub_id}",
			}
		)

	# Ensure all subscriptions declared in customer JSON are represented,
	# even when they are not visible to the current principal.
	for entry in customer_entries:
		sub_id = (entry.get("subscription_id") or "").strip()
		if not sub_id or sub_id in seen_sub_ids:
			continue
		rows.append(
			{
				"tenant_id": (entry.get("tenant_id") or args.tenant_id or "").strip(),
				"subscription_id": sub_id,
				"subscription_name": (entry.get("subscription_name") or "").strip(),
				"subscription_status": "NotVisible",
				"customer_status": str(entry.get("status") or "").strip(),
				"management_group": "",
				"overall_security_score": "",
				"resource_count": 0,
				"snapshot_of": snapshot_utc,
				"azure_portal_url": f"https://portal.azure.com/#@/resource/subscriptions/{sub_id}",
			}
		)

	rows.sort(key=lambda r: (r["management_group"], r["subscription_name"], r["subscription_id"]))

	out_dir = Path(args.output_dir)
	out_dir.mkdir(parents=True, exist_ok=True)

	tenant_label = (args.tenant_id or "tenant").replace("-", "")[:12]
	base_name = f"subscriptions_{tenant_label}_{date.today().strftime('%Y%m%d')}"
	output_file = out_dir / f"{base_name}.csv"
	json_file = out_dir / f"{base_name}.json"
	payload = {
		"tenant_id": args.tenant_id,
		"subscriptions": rows,
	}
	if args.output_format in ("csv", "both"):
		write_csv(
			output_file,
			rows,
			[
				"tenant_id",
				"subscription_id",
				"subscription_name",
				"subscription_status",
				"customer_status",
				"management_group",
				"overall_security_score",
				"resource_count",
				"snapshot_of",
				"azure_portal_url",
			],
		)
	if args.output_format in ("json", "both"):
		write_json(json_file, payload)

	if args.json:
		print(json.dumps(payload, indent=2))

	print(f"Done. Subscriptions fetched: {len(rows)}")
	if args.output_format in ("csv", "both"):
		print(f"CSV written: {output_file}")
	if args.output_format in ("json", "both"):
		print(f"JSON written: {json_file}")


if __name__ == "__main__":
	main()
