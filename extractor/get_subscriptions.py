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
from datetime import date
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
		scores[sub_id] = f"{score:.2f}" if score is not None else ""

	return scores


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

	print("Resolving management groups...")
	mg_mapping = fetch_management_group_mapping(token)

	print("Resolving overall security scores...")
	security_score_mapping = fetch_security_score_mapping(
		token,
		[sub.get("subscription_id", "") for sub in subscriptions],
	)

	rows = []
	for sub in subscriptions:
		sub_id = sub["subscription_id"]
		rows.append(
			{
				"tenant_id": sub["tenant_id"] or (args.tenant_id or ""),
				"subscription_id": sub_id,
				"subscription_name": sub["subscription_name"],
				"subscription_status": sub["subscription_state"],
				"management_group": mg_mapping.get(sub_id, ""),
				"overall_security_score": security_score_mapping.get(sub_id, ""),
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
				"management_group",
				"overall_security_score",
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
