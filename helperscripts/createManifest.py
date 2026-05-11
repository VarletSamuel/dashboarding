#!/usr/bin/env python3
"""Create or update dashboard manifest.json from report files.

Behavior:
- Scans a report folder for known CSV/TXT files.
- Creates a new manifest when none exists.
- Merges into an existing manifest when rerun.
- Keeps only the latest `summary` file per dashboard.
- Appends `timeseries` and `costs_by_resource` files (deduplicated by filename).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


KNOWN_DASHBOARDS: Dict[str, Tuple[str, str]] = {
	"app_service_plans": ("appServicePlans", "App Service Plans"),
	"container_apps": ("containerApps", "Container Apps"),
	"postgresql": ("postgresql", "PostgreSQL"),
	"virtual_machines": ("virtualMachines", "Virtual Machines"),
	"eventhub": ("eventhubs", "EventHub"),
	"eventhubs": ("eventhubs", "EventHub"),
	"daily_costs": ("azureCosts", "Azure Costs"),
}

TYPE_SORT_ORDER = {
	"summary": 0,
	"timeseries": 1,
	"costs_by_resource": 2,
	"costs": 2,
}

DASHBOARD_ORDER = [
	"appServicePlans",
	"containerApps",
	"postgresql",
	"virtualMachines",
	"eventhubs",
	"azureCosts",
]


def snake_to_camel(value: str) -> str:
	parts = [p for p in value.split("_") if p]
	if not parts:
		return value
	return parts[0] + "".join(p.capitalize() for p in parts[1:])


def metric_to_dashboard(metric: str) -> Tuple[str, str]:
	if metric in KNOWN_DASHBOARDS:
		return KNOWN_DASHBOARDS[metric]
	return snake_to_camel(metric), metric.replace("_", " ").title()


def parse_iso_date(value: Optional[str]) -> datetime:
	if not value:
		return datetime.min.replace(tzinfo=timezone.utc)
	try:
		return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
	except ValueError:
		return datetime.min.replace(tzinfo=timezone.utc)


def parse_date_from_filename(filename: str) -> Tuple[Optional[str], Optional[str]]:
	match = re.search(r"(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})", filename)
	if match:
		return match.group(1), match.group(2)
	return None, None


def parse_report_file(file_path: Path) -> Optional[Dict[str, Any]]:
	name = file_path.name

	daily_costs_pattern = re.compile(
		r"^(?P<customer>[A-Za-z0-9]+)_daily_costs_(?P<date_from>\d{4}-\d{2}-\d{2})_(?P<date_to>\d{4}-\d{2}-\d{2})\.csv$"
	)
	dashboard_pattern = re.compile(
		r"^(?P<customer>[A-Za-z0-9]+)_(?P<metric>.+)_(?P<file_type>summary|timeseries)_(?P<date_from>\d{4}-\d{2}-\d{2})_(?P<date_to>\d{4}-\d{2}-\d{2})\.csv$"
	)

	match = daily_costs_pattern.match(name)
	if match:
		customer = match.group("customer")
		date_from = match.group("date_from")
		date_to = match.group("date_to")
		dashboard_id, dashboard_title = metric_to_dashboard("daily_costs")
		return {
			"customer": customer,
			"kind": "dashboard_file",
			"dashboard_id": dashboard_id,
			"dashboard_title": dashboard_title,
			"file": {
				"type": "costs_by_resource",
				"filename": name,
				"date_from": date_from,
				"date_to": date_to,
			},
		}

	match = dashboard_pattern.match(name)
	if match:
		customer = match.group("customer")
		metric = match.group("metric")
		file_type = match.group("file_type")
		date_from = match.group("date_from")
		date_to = match.group("date_to")
		dashboard_id, dashboard_title = metric_to_dashboard(metric)
		return {
			"customer": customer,
			"kind": "dashboard_file",
			"dashboard_id": dashboard_id,
			"dashboard_title": dashboard_title,
			"file": {
				"type": file_type,
				"filename": name,
				"date_from": date_from,
				"date_to": date_to,
			},
		}

	if re.match(r"^subscriptions_.*\.csv$", name):
		return {
			"kind": "other_file",
			"other": {
				"type": "subscriptions",
				"filename": name,
				"description": "Subscription metadata and tenant mapping",
			},
		}

	if re.match(r"^[A-Za-z0-9]+_log\.txt$", name):
		customer = name.split("_", 1)[0]
		return {
			"customer": customer,
			"kind": "other_file",
			"other": {
				"type": "log",
				"filename": name,
				"description": "Extraction execution log",
			},
		}

	return None


def load_existing_manifest(path: Path) -> Dict[str, Any]:
	if not path.exists():
		return {}
	try:
		return json.loads(path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		raise RuntimeError(f"Could not read existing manifest: {path} ({exc})") from exc


def dedupe_by_filename(files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
	by_name: Dict[str, Dict[str, Any]] = {}
	for item in files:
		if "filename" in item:
			by_name[item["filename"]] = item
	return list(by_name.values())


def file_sort_key(file_entry: Dict[str, Any]) -> Tuple[int, datetime, str]:
	file_type = file_entry.get("type", "")
	date_from = file_entry.get("date_from")
	date_to = file_entry.get("date_to")
	if not (date_from and date_to):
		parsed_from, parsed_to = parse_date_from_filename(file_entry.get("filename", ""))
		date_from = date_from or parsed_from
		date_to = date_to or parsed_to

	return (
		TYPE_SORT_ORDER.get(file_type, 99),
		parse_iso_date(date_to or date_from),
		file_entry.get("filename", ""),
	)


def pick_latest_summary(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
	if not entries:
		return None

	def summary_key(item: Dict[str, Any]) -> Tuple[datetime, str]:
		date_from = item.get("date_from")
		date_to = item.get("date_to")
		if not (date_from and date_to):
			parsed_from, parsed_to = parse_date_from_filename(item.get("filename", ""))
			date_from = date_from or parsed_from
			date_to = date_to or parsed_to
		return parse_iso_date(date_to or date_from), item.get("filename", "")

	return max(entries, key=summary_key)


def merge_dashboard_files(
	existing_files: List[Dict[str, Any]],
	new_files: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
	existing_files = dedupe_by_filename(existing_files)
	new_files = dedupe_by_filename(new_files)

	existing_summaries = [f for f in existing_files if f.get("type") == "summary"]
	new_summaries = [f for f in new_files if f.get("type") == "summary"]
	latest_summary = pick_latest_summary(existing_summaries + new_summaries)

	existing_non_summary = [f for f in existing_files if f.get("type") != "summary"]
	new_non_summary = [f for f in new_files if f.get("type") != "summary"]
	merged_non_summary = dedupe_by_filename(existing_non_summary + new_non_summary)

	merged = []
	if latest_summary is not None:
		merged.append(latest_summary)
	merged.extend(sorted(merged_non_summary, key=file_sort_key))
	return merged


def merge_other_files(
	existing_other: List[Dict[str, Any]],
	new_other: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
	by_type: Dict[str, List[Dict[str, Any]]] = {}

	for item in existing_other + new_other:
		file_type = item.get("type", "unknown")
		by_type.setdefault(file_type, []).append(item)

	merged: List[Dict[str, Any]] = []
	for file_type, items in by_type.items():
		deduped = dedupe_by_filename(items)
		if file_type in {"subscriptions", "log"}:
			# Keep only the latest metadata/log file when rerunning.
			latest = max(deduped, key=lambda x: (parse_iso_date(parse_date_from_filename(x.get("filename", ""))[1]), x.get("filename", "")))
			merged.append(latest)
		else:
			merged.extend(sorted(deduped, key=lambda x: x.get("filename", "")))

	return sorted(merged, key=lambda x: (x.get("type", ""), x.get("filename", "")))


def has_csv_data_rows(file_path: Path) -> bool:
	"""Return True only if the CSV has at least one non-empty data row beyond the header."""
	try:
		with file_path.open(newline="", encoding="utf-8-sig") as fh:
			reader = csv.reader(fh)
			# Skip header row
			try:
				next(reader)
			except StopIteration:
				return False  # completely empty
			# Check for at least one data row with content
			for row in reader:
				if any(cell.strip() for cell in row):
					return True
		return False
	except OSError:
		return False


def build_manifest(report_dir: Path, existing_manifest: Dict[str, Any]) -> Dict[str, Any]:
	discovered = []
	for file_path in report_dir.iterdir():
		if file_path.name == "manifest.json" or not file_path.is_file():
			continue
		if file_path.suffix.lower() == ".csv" and not has_csv_data_rows(file_path):
			print(f"  ⚠  Skipping empty/header-only CSV: {file_path.name}")
			continue
		parsed = parse_report_file(file_path)
		if parsed:
			discovered.append(parsed)

	discovered_customer = next((item.get("customer") for item in discovered if item.get("customer")), None)
	customer = discovered_customer or existing_manifest.get("customer") or report_dir.name.split("_", 1)[0]

	existing_dashboards = existing_manifest.get("dashboards", []) if isinstance(existing_manifest.get("dashboards"), list) else []
	by_dashboard: Dict[str, Dict[str, Any]] = {
		d.get("id"): {
			"id": d.get("id"),
			"title": d.get("title") or d.get("id", ""),
			"files": list(d.get("files", [])) if isinstance(d.get("files"), list) else [],
		}
		for d in existing_dashboards
		if d.get("id")
	}

	new_dashboard_files: Dict[str, List[Dict[str, Any]]] = {}
	new_dashboard_titles: Dict[str, str] = {}
	new_other_files: List[Dict[str, Any]] = []

	for item in discovered:
		if item.get("kind") == "dashboard_file":
			dashboard_id = item["dashboard_id"]
			new_dashboard_titles[dashboard_id] = item["dashboard_title"]
			new_dashboard_files.setdefault(dashboard_id, []).append(item["file"])
		elif item.get("kind") == "other_file":
			new_other_files.append(item["other"])

	for dashboard_id, files in new_dashboard_files.items():
		existing = by_dashboard.get(dashboard_id, {"id": dashboard_id, "title": new_dashboard_titles.get(dashboard_id, dashboard_id), "files": []})
		existing["title"] = new_dashboard_titles.get(dashboard_id, existing.get("title") or dashboard_id)
		existing["files"] = merge_dashboard_files(existing.get("files", []), files)
		by_dashboard[dashboard_id] = existing

	dashboard_list = sorted(
		by_dashboard.values(),
		key=lambda d: (
			DASHBOARD_ORDER.index(d["id"]) if d["id"] in DASHBOARD_ORDER else 999,
			d["id"],
		),
	)

	existing_other = existing_manifest.get("other_files", []) if isinstance(existing_manifest.get("other_files"), list) else []
	other_files = merge_other_files(existing_other, new_other_files)

	all_dates_from: List[str] = []
	all_dates_to: List[str] = []
	for dashboard in dashboard_list:
		for file_entry in dashboard.get("files", []):
			date_from = file_entry.get("date_from")
			date_to = file_entry.get("date_to")
			if not (date_from and date_to):
				parsed_from, parsed_to = parse_date_from_filename(file_entry.get("filename", ""))
				date_from = date_from or parsed_from
				date_to = date_to or parsed_to
			if date_from:
				all_dates_from.append(date_from)
			if date_to:
				all_dates_to.append(date_to)

	if all_dates_from and all_dates_to:
		date_range = {
			"from": min(all_dates_from),
			"to": max(all_dates_to),
		}
	else:
		date_range = existing_manifest.get("date_range", {"from": None, "to": None})

	return {
		"customer": customer,
		"generated_at": datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"),
		"date_range": date_range,
		"dashboards": dashboard_list,
		"other_files": other_files,
	}


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Create or update manifest.json from files in a report folder.",
		add_help=False,
	)
	parser.add_argument(
		"-h",
		"--help",
		"-?",
		action="help",
		help="Show this help message and exit.",
	)
	parser.add_argument(
		"report_folder",
		help="Path to the report folder containing CSV/TXT files.",
	)
	parser.add_argument(
		"--manifest-path",
		help="Optional output manifest path (default: <report_folder>/manifest.json).",
	)
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	report_dir = Path(args.report_folder).resolve()
	if not report_dir.exists() or not report_dir.is_dir():
		print(f"Error: report folder does not exist or is not a directory: {report_dir}")
		return 1

	manifest_path = Path(args.manifest_path).resolve() if args.manifest_path else report_dir / "manifest.json"
	existing = load_existing_manifest(manifest_path)

	manifest = build_manifest(report_dir, existing)
	manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

	print(f"Manifest written: {manifest_path}")
	print(f"Customer: {manifest.get('customer')}")
	print(f"Dashboards: {len(manifest.get('dashboards', []))}")
	print(f"Other files: {len(manifest.get('other_files', []))}")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
