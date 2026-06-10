#!/usr/bin/env python3
"""
managedServiceWrapper.py
========================
Orchestrator that logs in to every Azure tenant for a customer once, then
runs all reporting scripts sequentially with --skip-login so the az session
is reused across all of them.

Before execution, the wrapper validates Azure CLI availability, checks the
declared Python dependencies for each selected extractor, and smoke-tests each
extractor with --help to catch startup/import issues early.

If a requirement is missing or a selected extractor cannot start, the wrapper
stops and prints a consolidated remediation guide plus a detailed error log.

Scripts run (in order):
    1. get_subscriptions.py
    2. get_daily_costs.py
    3. get_reservations_commitments.py
    4. get_quota_consumption.py
    5. get_virtualmachines.py
    6. get_containerApps.py
    7. get_appserviceplans.py
    8. get_storage_accounts.py
    9. get_keyvaults.py
    10. get_app_secrets_expiry.py
    11. get_postgresql.py
    12. get_sql.py
    13. get_eventhubnamespaces.py
    14. get_loganalyticsworkspace.py
    15. fill_qc_template.py  (optional — add --fill-qc to enable)

Extractor scripts are expected in the sibling `extractor/` folder.
Output is written to <output-dir>/<CUSTOMER>/.

Usage
-----
    python managedServiceWrapper.py -c CUST
    python managedServiceWrapper.py -c CUST -i ../customers/CUST.json --output-dir ../reports
    python managedServiceWrapper.py -c CUST --from 2026-02-01 --to 2026-04-20
    python managedServiceWrapper.py -c CUST --lookback PT6H
    python managedServiceWrapper.py -c CUST --skip get_subscriptions get_daily_costs get_reservations_commitments get_quota_consumption get_virtualmachines get_containerApps get_appserviceplans get_storage_accounts get_keyvaults get_app_secrets_expiry get_postgresql get_sql get_eventhubnamespaces get_loganalyticsworkspace
    python managedServiceWrapper.py -c CUST --skip-login
    python managedServiceWrapper.py -c CUST --sp-client-id <appId> --sp-client-secret <secret>
    python managedServiceWrapper.py -c CUST --sp-client-id <appId> --sp-certificate /path/to/cert.pem
    python managedServiceWrapper.py -c CUST --fill-qc
    python managedServiceWrapper.py -c CUST --fill-qc --qc-template-version 1.1
    python managedServiceWrapper.py -c CUST --fill-qc --storage-connection-string "<conn>"
"""

import argparse
import ast
import csv
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# On Windows the Azure CLI wrapper is az.cmd, not az
AZ_CMD = "az.cmd" if sys.platform == "win32" else "az"

# This file lives in helperscripts/; extractor scripts are one level up in extractor/
SCRIPT_DIR = Path(__file__).parent
EXTRACTOR_DIR = SCRIPT_DIR.parent / "extractor"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
RED = "\033[31m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"


def _supports_color(stream) -> bool:
    return hasattr(stream, "isatty") and stream.isatty()


def _colorize(text: str, color: str) -> str:
    if not text:
        return text
    if not _supports_color(sys.__stderr__):
        return text
    return f"{color}{text}{RESET}"


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE_RE.sub("", text)


def _print_error(message: str) -> None:
    sys.stderr.write(_colorize(f"{message}\n", RED))
    sys.stderr.flush()


def _print_warning(message: str) -> None:
    sys.stdout.write(_colorize(f"{message}\n", YELLOW))
    sys.stdout.flush()


def _print_success(message: str) -> None:
    sys.stdout.write(_colorize(f"{message}\n", GREEN))
    sys.stdout.flush()


class _Tee:
    """Duplicate writes to *stream* into *log_file* as well."""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log = log_file

    def write(self, data):
        self._stream.write(data)
        self._log.write(_strip_ansi(data))

    def flush(self):
        self._stream.flush()
        self._log.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _banner(text: str, char: str = "═", width: int = 70) -> None:
    print(f"\n{char * width}")
    print(f"  {text}")
    print(f"{char * width}")


def resolve_customer_csv(customer_code: str, input_override: str | None) -> Path:
    """Resolve the customer JSON path from override or ../customers/<CUSTOMER>.json."""
    if input_override:
        json_path = Path(input_override)
    else:
        json_path = SCRIPT_DIR.parent / "customers" / f"{customer_code}.json"

    if not json_path.exists():
        _print_error(f"ERROR: customer file not found: {json_path}")
        sys.exit(1)

    return json_path


def read_tenants(json_path: Path) -> list[str]:
    """Return the ordered list of unique tenant IDs from the customer JSON."""
    if not json_path.exists():
        _print_error(f"ERROR: customer file not found: {json_path}")
        sys.exit(1)

    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)

    seen: set[str] = set()
    tenant_ids: list[str] = []
    for entry in data.get("azure", []):
        tenant = (entry.get("tenant_id") or "").strip()
        if tenant and tenant not in seen:
            seen.add(tenant)
            tenant_ids.append(tenant)

    if not tenant_ids:
        _print_error(f"ERROR: No tenants found in {json_path}")
        sys.exit(1)

    return tenant_ids


def read_customer_json(json_path: Path) -> dict:
    """Return the full parsed customer JSON dict."""
    with open(json_path, encoding="utf-8") as f:
        return json.load(f)


def filter_active_azure_entries(customer_data: dict) -> tuple[dict, int, int]:
    """Return customer data containing only azure entries with status=Active.

    Entries with a status different from Active (case-insensitive) are skipped.
    """
    azure_entries = customer_data.get("azure", [])
    if not isinstance(azure_entries, list):
        return customer_data, 0, 0

    total_entries = len(azure_entries)
    active_entries = []

    for entry in azure_entries:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status", "")).strip().lower()
        if status == "active":
            active_entries.append(entry)

    filtered = dict(customer_data)
    filtered["azure"] = active_entries
    return filtered, total_entries, len(active_entries)


def write_filtered_customer_json(filtered_data: dict, out_dir: Path, customer: str) -> Path:
    """Write filtered customer JSON used by downstream extractors."""
    # Keep filename stem stable so child extractors preserve expected output names.
    filtered_path = out_dir / f"{customer}.json"
    with open(filtered_path, "w", encoding="utf-8") as handle:
        json.dump(filtered_data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return filtered_path


def az_login_tenant(
    tenant_id: str,
    sp_client_id: str | None = None,
    sp_client_secret: str | None = None,
    sp_certificate: str | None = None,
) -> bool:
    """Login to *tenant_id*.

    When *sp_client_id* is supplied, uses ``az login --service-principal``
    with either a client secret or a certificate (PEM path).  Falls back to
    interactive browser login otherwise.
    """
    W = 70
    print(f"\n{'─' * W}")
    if sp_client_id:
        auth_method = "certificate" if sp_certificate else "client secret"
        print(f"  Logging in to tenant: {tenant_id}  (service principal / {auth_method})")
    else:
        print(f"  Logging in to tenant: {tenant_id}")
    print(f"{'─' * W}")

    if sp_client_id:
        if sp_certificate:
            cmd = [
                AZ_CMD, "login", "--service-principal",
                "--username", sp_client_id,
                "--certificate", sp_certificate,
                "--tenant", tenant_id,
            ]
        else:
            cmd = [
                AZ_CMD, "login", "--service-principal",
                "--username", sp_client_id,
                "--password", sp_client_secret,
                "--tenant", tenant_id,
            ]
    else:
        cmd = [AZ_CMD, "login", "--tenant", tenant_id]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(_colorize(result.stderr, RED))
    if result.returncode != 0:
        _print_error(f"  ✗  Login failed for tenant {tenant_id}")
    else:
        _print_success(f"  ✓  Logged in to tenant {tenant_id}")
    return result.returncode == 0


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    return env


def _stream_pipe(pipe, writer, color: str | None = None) -> None:
    """Forward lines from *pipe* to *writer* in real time (used in a thread)."""
    try:
        for line in iter(pipe.readline, ""):
            writer.write(_colorize(line, color) if color else line)
            writer.flush()
    finally:
        pipe.close()


def run_script(script_path: Path, script_args: list[str], label: str) -> bool:
    """Run *script_path* as a subprocess, streaming output in real time; return True on exit code 0."""
    cmd = [sys.executable, str(script_path)] + script_args
    _banner(f"Running: {label}", char="─")
    print(f"  $ {' '.join(cmd)}\n")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_child_env(),
    )
    t_out = threading.Thread(target=_stream_pipe, args=(proc.stdout, sys.stdout))
    t_err = threading.Thread(target=_stream_pipe, args=(proc.stderr, sys.stderr, RED))
    t_out.start()
    t_err.start()
    t_out.join()
    t_err.join()
    returncode = proc.wait()
    if returncode != 0:
        _print_error(f"\n  ✗  {label} exited with code {returncode}")
        return False
    _print_success(f"\n  ✓  {label} completed successfully")
    return True


def run_subscriptions_script(
    script_path: Path,
    tenant_ids: list[str],
    common_args: list[str],
    label: str,
) -> bool:
    """Run get_subscriptions.py once per tenant using the requested sequence."""
    _banner(f"Running: {label}", char="─")

    success = True
    for tenant_id in tenant_ids:
        tenant_args = common_args + ["--tenant-id", tenant_id]
        print(f"  Tenant: {tenant_id}")
        cmd = [sys.executable, str(script_path)] + tenant_args
        print(f"  $ {' '.join(cmd)}\n")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(),
        )
        t_out = threading.Thread(target=_stream_pipe, args=(proc.stdout, sys.stdout))
        t_err = threading.Thread(target=_stream_pipe, args=(proc.stderr, sys.stderr, RED))
        t_out.start()
        t_err.start()
        t_out.join()
        t_err.join()
        returncode = proc.wait()
        if returncode != 0:
            _print_error(f"\n  ✗  {label} failed for tenant {tenant_id} with code {returncode}")
            success = False
            break

    if success:
        _print_success(f"\n  ✓  {label} completed successfully")
    return success


def _extract_required_modules(script_path: Path) -> dict[str, str]:
    """Read a script's top-level _REQUIRED dict without importing the script."""
    try:
        source = script_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(script_path))
    except Exception:
        return {}

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "_REQUIRED":
                try:
                    value = ast.literal_eval(node.value)
                except Exception:
                    return {}
                if isinstance(value, dict):
                    return {str(module): str(package) for module, package in value.items()}
    return {}


def _check_python_modules(required: dict[str, str]) -> tuple[list[dict], list[dict]]:
    present: list[dict] = []
    missing: list[dict] = []

    for module_name, package_name in required.items():
        try:
            importlib.import_module(module_name)
            present.append({"module": module_name, "package": package_name})
        except Exception as exc:
            missing.append(
                {
                    "module": module_name,
                    "package": package_name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    return present, missing


def _smoke_test_script(script_path: Path) -> tuple[bool, str]:
    """Run the script with --help to catch import/startup failures early."""
    result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_child_env(),
    )
    combined_output = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode == 0, combined_output


def _has_sas_in_connection_string(connection_string: str) -> bool:
    parts: dict[str, str] = {}
    for token in connection_string.split(";"):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        parts[key.strip().lower()] = value.strip()
    return bool(parts.get("sharedaccesssignature", ""))


def upload_directory_to_blob_storage(
    source_dir: Path,
    connection_string: str,
    container_name: str,
    blob_prefix: str,
) -> int:
    """Upload all files from *source_dir* to Azure Blob Storage and return file count."""
    try:
        from azure.core.exceptions import ResourceExistsError  # type: ignore[import-not-found]
        from azure.storage.blob import BlobServiceClient  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Missing Azure Blob SDK. Install with: "
            f"{sys.executable} -m pip install azure-storage-blob"
        ) from exc

    if not _has_sas_in_connection_string(connection_string):
        raise ValueError(
            "The provided storage connection string does not include SharedAccessSignature."
        )

    blob_service = BlobServiceClient.from_connection_string(connection_string)
    container_client = blob_service.get_container_client(container_name)

    try:
        container_client.create_container()
    except ResourceExistsError:
        pass

    prefix = blob_prefix.strip("/")
    uploaded = 0

    for file_path in source_dir.rglob("*"):
        if not file_path.is_file():
            continue

        rel_path = file_path.relative_to(source_dir).as_posix()
        blob_name = f"{prefix}/{rel_path}" if prefix else rel_path

        with open(file_path, "rb") as data:
            container_client.upload_blob(name=blob_name, data=data, overwrite=True)
        uploaded += 1

    return uploaded


def _get_manifest_builder():
    """Dynamically import build_manifest / load_existing_manifest from helperscripts."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "createManifest", SCRIPT_DIR / "createManifest.py"
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot locate helperscripts/createManifest.py at {SCRIPT_DIR}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.build_manifest, mod.load_existing_manifest


def _download_manifest_from_blob(
    connection_string: str,
    container_name: str,
    blob_prefix: str,
) -> dict:
    """Try to download and parse an existing manifest.json from blob storage.

    Returns the parsed dict on success, or an empty dict when the blob does not
    exist or any error occurs (non-fatal — a fresh manifest is generated instead).
    """
    try:
        from azure.storage.blob import BlobServiceClient  # type: ignore[import-not-found]
    except ImportError:
        return {}

    try:
        client = BlobServiceClient.from_connection_string(connection_string)
        prefix = blob_prefix.strip("/")
        blob_name = f"{prefix}/manifest.json" if prefix else "manifest.json"
        blob_client = client.get_blob_client(container=container_name, blob=blob_name)
        raw = blob_client.download_blob().readall().decode("utf-8")
        return json.loads(raw)
    except Exception:
        return {}


def run_manifest_upload_script(
    report_dir: Path,
    connection_input: str,
    container_name: str,
    blob_prefix: str,
) -> bool:
    """Run upload_manifest_reports.py and return True on success."""
    upload_script = SCRIPT_DIR / "upload_manifest_reports.py"
    if not upload_script.exists():
        _print_warning(f"  ⚠  Upload script not found: {upload_script}")
        return False

    cmd = [
        sys.executable,
        str(upload_script),
        str(report_dir),
        "--connection-string",
        connection_input,
        "--container",
        container_name,
        "--prefix",
        blob_prefix,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_child_env(),
    )
    if result.stdout:
        sys.stdout.write(result.stdout)
    if result.stderr:
        sys.stderr.write(_colorize(result.stderr, RED))
    return result.returncode == 0


def run_dependency_preflight(selected_scripts: list[tuple[str, str, list[str]]]) -> tuple[bool, dict]:
    report: dict = {
        "python_executable": sys.executable,
        "az_cli": {"available": False, "path": shutil.which(AZ_CMD) or ""},
        "scripts": [],
    }

    az_path = shutil.which(AZ_CMD)
    report["az_cli"]["available"] = bool(az_path)
    all_ok = bool(az_path)

    for stem, label, _extra in selected_scripts:
        script_path = EXTRACTOR_DIR / f"{stem}.py"
        script_report = {
            "stem": stem,
            "label": label,
            "path": str(script_path),
            "exists": script_path.exists(),
            "required": {},
            "present": [],
            "missing": [],
            "smoke_test_ok": False,
            "smoke_test_output": "",
        }

        if not script_path.exists():
            script_report["missing"].append(
                {"module": "<script>", "package": script_path.name, "error": "Script file not found"}
            )
            report["scripts"].append(script_report)
            all_ok = False
            continue

        required = _extract_required_modules(script_path)
        script_report["required"] = required
        present, missing = _check_python_modules(required)
        script_report["present"] = present
        script_report["missing"] = missing

        smoke_ok, smoke_output = _smoke_test_script(script_path)
        script_report["smoke_test_ok"] = smoke_ok
        script_report["smoke_test_output"] = smoke_output

        if missing or not smoke_ok:
            all_ok = False

        report["scripts"].append(script_report)

    return all_ok, report


def print_dependency_failure_report(report: dict) -> None:
    _banner("Dependency Preflight Failed")
    print(f"  Python executable : {report.get('python_executable', '')}")

    az_cli = report.get("az_cli", {})
    print(f"  Azure CLI        : {'OK' if az_cli.get('available') else 'MISSING'}")
    if az_cli.get("path"):
        print(f"  Azure CLI path   : {az_cli.get('path')}")

    if not az_cli.get("available"):
        print("\n  Missing prerequisite:")
        print(f"    - {AZ_CMD} is not available on PATH")

    missing_packages: dict[str, set[str]] = {}
    startup_failures = []

    for script in report.get("scripts", []):
        for item in script.get("missing", []):
            package_name = item.get("package", "")
            module_name = item.get("module", "")
            if package_name:
                missing_packages.setdefault(package_name, set()).add(module_name)

        if not script.get("smoke_test_ok"):
            startup_failures.append(script)

    if missing_packages:
        print("\n  Missing Python requirements:")
        for package_name in sorted(missing_packages):
            modules = ", ".join(sorted(missing_packages[package_name]))
            print(f"    - {package_name}  (import(s): {modules})")

    if startup_failures:
        print("\n  Script startup failures:")
        for script in startup_failures:
            print(f"    - {script['stem']}.py")

    print("\n  How to fix:")
    print("    1. Install missing packages into this Python:")
    if missing_packages:
        package_list = " ".join(sorted(missing_packages))
        print(f"       {sys.executable} -m pip install {package_list}")
    else:
        print("       No missing pip packages were detected.")
    print("    2. If Azure CLI is missing, install it and reopen the shell.")
    print("    3. For startup failures without missing packages, inspect the detailed log below.")

    print("\n  Detailed error log:")
    print("  " + "-" * 66)
    for script in report.get("scripts", []):
        if script.get("missing") or not script.get("smoke_test_ok"):
            print(f"  Script: {script['stem']}.py")
            print(f"  Path  : {script['path']}")
            for item in script.get("missing", []):
                print(
                    f"  Missing: package={item.get('package', '')} "
                    f"module={item.get('module', '')} error={item.get('error', '')}"
                )
            if not script.get("smoke_test_ok"):
                print("  Smoke test output:")
                output = script.get("smoke_test_output", "") or "<no output>"
                for line in output.splitlines()[:80]:
                    print(f"    {line}")
            print("  " + "-" * 66)


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run all managed-service reporting scripts for a customer.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    usage="""%(prog)s\n  -c CUSTOMER\n  [-i INPUT]\n  [--output-dir OUTPUT_DIR]\n  [--output-format {csv,json,both}]\n  [--storage-connection-string CONNECTION_STRING]\n  [--storage-container CONTAINER]\n  [--storage-prefix PREFIX]\n  [--skip-login]\n  [--sp-client-id APP_ID]\n  [--sp-client-secret SECRET]\n  [--sp-certificate CERT_PATH]\n  [--from DATE_FROM]\n  [--to DATE_TO]\n  [--lookback LOOKBACK]\n  [--no-utilisation]\n  [--skip SCRIPT [SCRIPT ...]]\n  [--only SCRIPT [SCRIPT ...]]""",
        epilog="""
examples:
  %(prog)s -c CUST
        %(prog)s -c CUST -i ../customers/CUST.json --output-dir ./reports
  %(prog)s -c CUST --from 2026-02-01 --to 2026-04-20
  %(prog)s -c CUST --lookback PT6H
    %(prog)s -c CUST --skip get_subscriptions get_reservations_commitments get_eventhubnamespaces
  %(prog)s -c CUST --skip-login
        """,
    )

    # ── Required ──────────────────────────────────────────────────────────────
    parser.add_argument(
        "-h", "--help", "-?",
        action="help",
        help="Show this help message and exit.",
    )
    parser.add_argument(
        "-c", "--customer",
        required=True,
        help="Customer code (e.g. ARTC, DATS, SYNH).",
    )

    # ── Common ────────────────────────────────────────────────────────────────
    parser.add_argument(
        "-i", "--input",
        default=None,
        help="Optional override for customer JSON path. Default: ../customers/<CUSTOMER>.json",
    )
    parser.add_argument(
        "--output-dir",
           default=None,
        help="Root output directory. Files are written to sub-folder <CUSTOMER> "
               "(default: ../reports relative to this script).",
    )
    parser.add_argument(
        "--output-format",
        choices=("csv", "json", "both"),
        default="csv",
        help="File output format for child extractors: csv, json, or both (default: csv).",
    )
    parser.add_argument(
        "--storage-connection-string",
        default=None,
           help="Optional storage auth input for upload stage. Accepts either a "
               "SAS URL or a connection string containing SharedAccessSignature.",
    )
    parser.add_argument(
        "--storage-container",
        default="managed-service-reports",
        help="Blob container used with --storage-connection-string "
             "(default: managed-service-reports).",
    )
    parser.add_argument(
        "--storage-prefix",
        default=None,
        help="Optional blob path prefix. Default: <CUSTOMER>.",
    )
    parser.add_argument(
        "--skip-login",
        action="store_true",
        help="Skip 'az login --tenant'. Use when you are already logged in to all tenants.",
    )

    # ── Service Principal (App Registration) auth ─────────────────────────────
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

    # ── Date / time range (forwarded to cost and metrics scripts) ─────────────
    parser.add_argument(
        "--from", dest="date_from", default=None,
           help="Start date YYYY-MM-DD (forwarded to get_daily_costs, "
               "get_eventhubnamespaces, get_containerApps, get_appserviceplans, get_postgresql). "
               "Default: first day of previous month.",
    )
    parser.add_argument(
        "--to", dest="date_to", default=None,
           help="End date YYYY-MM-DD (forwarded to get_daily_costs, "
               "get_eventhubnamespaces, get_containerApps, get_appserviceplans, get_postgresql). "
               "Default: today.",
    )
    parser.add_argument(
        "--lookback",
        default=None,
        help="Metrics lookback window: integer minutes or ISO-8601 duration like PT6H. "
                         "Forwarded to get_eventhubnamespaces, get_containerApps, get_appserviceplans, and get_postgresql. "
               "Overrides the default date window for those metrics scripts when --from/--to are not set.",
    )

    # ── Reservations / commitments ────────────────────────────────────────────
    parser.add_argument(
        "--no-utilisation",
        action="store_true",
        help="Skip RI utilisation fetch in get_reservations_commitments (faster run).",
    )

    # ── Selective execution ───────────────────────────────────────────────────
    parser.add_argument(
        "--skip",
        nargs="+",
        metavar="SCRIPT",
        default=[],
        help="One or more script names to skip (without .py extension). "
             "Choices: get_subscriptions  get_daily_costs  get_reservations_commitments  "
                             "get_quota_consumption  "
               "get_virtualmachines  get_containerApps  get_appserviceplans  get_storage_accounts  "
               "get_keyvaults  get_app_secrets_expiry  get_postgresql  get_sql  get_eventhubnamespaces  "
                             "get_loganalyticsworkspace",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="SCRIPT",
        default=[],
        help="Run ONLY the listed script(s) — overrides --skip.",
    )

    # ── Quality Controls document generation ──────────────────────────────────
    parser.add_argument(
        "--fill-qc",
        action="store_true",
        help="Run fill_qc_template.py after all extractors complete to generate a QC DOCX.",
    )
    parser.add_argument(
        "--qc-template",
        default=None,
        metavar="DOCX_PATH",
        help="Path to the QC template DOCX (forwarded to fill_qc_template.py).",
    )
    parser.add_argument(
        "--qc-template-version",
        default=None,
        metavar="VERSION",
        help="Template version suffix, e.g. 1.1 or SYNH (forwarded to fill_qc_template.py).",
    )
    parser.add_argument(
        "--qc-output",
        default=None,
        metavar="OUTPUT_PATH",
        help="Output path for the generated QC DOCX (forwarded to fill_qc_template.py).",
    )

    return parser


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    customer = args.customer.upper()
    customer_json_path = resolve_customer_csv(customer, args.input)
    output_root = Path(args.output_dir) if args.output_dir else (SCRIPT_DIR.parent / "reports")

    # ── Stable output directory per customer ─────────────────────────────────
    out_dir = output_root / customer
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Tee stdout + stderr to a log file ─────────────────────────────────────
    log_path = out_dir / f"{customer}_log.txt"
    _log_file = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, _log_file)
    sys.stderr = _Tee(sys.__stderr__, _log_file)

    _banner("Managed Service Wrapper")
    print(f"  Customer    : {customer}")
    print(f"  Input file  : {customer_json_path}")
    print(f"  Output dir  : {out_dir}")
    print(f"  Run date    : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # ── Read + filter customer JSON based on azure status ─────────────────────
    customer_data = read_customer_json(customer_json_path)
    filtered_customer_data, total_azure_entries, active_azure_entries = filter_active_azure_entries(customer_data)
    skipped_azure_entries = max(total_azure_entries - active_azure_entries, 0)

    print(
        f"  Azure scope : {active_azure_entries}/{total_azure_entries} active "
        f"({skipped_azure_entries} skipped by status)"
    )

    if active_azure_entries == 0:
        _print_error("ERROR: No active azure subscriptions found in customer JSON.")
        sys.exit(1)

    filtered_customer_json_path = write_filtered_customer_json(filtered_customer_data, out_dir, customer)
    print(f"  Active file : {filtered_customer_json_path}")

    # ── Auto-read AppReg client_id from JSON if not given on CLI ──────────────
    json_client_id = (customer_data.get("authentication") or {}).get("client_id", "")
    if json_client_id and not args.sp_client_id and (args.sp_client_secret or args.sp_certificate):
        args.sp_client_id = json_client_id
        print(f"  App Reg     : {json_client_id} (from customer JSON)")
    elif json_client_id and not args.sp_client_id:
        print(f"  App Reg     : {json_client_id} (available in customer JSON, not used without secret/certificate)")

    tenant_ids = read_tenants(filtered_customer_json_path)

    # ── Tenant login ──────────────────────────────────────────────────────────
    if not args.skip_login:
        print(f"\n  Tenants ({len(tenant_ids)}): {', '.join(tenant_ids)}")

        for tenant_id in tenant_ids:
            if not az_login_tenant(tenant_id, args.sp_client_id, args.sp_client_secret, args.sp_certificate):
                print(f"\nERROR: Login to tenant {tenant_id} failed. Aborting.")
                sys.exit(1)

        _print_success(f"\n  ✓  Logged in to all {len(tenant_ids)} tenant(s).")
    else:
        print("\n  (--skip-login: reusing current az session)")

    # ── Argument sets forwarded to each script ────────────────────────────────
    common = [
        "-i", str(filtered_customer_json_path),
        "--skip-login",
        "--output-dir", str(out_dir),
        "--output-format", args.output_format,
    ]

    # Forward SP credentials to child scripts so they can re-login per tenant
    # when the child scripts manage their own per-tenant login loop.
    if args.sp_client_id:
        common += ["--sp-client-id", args.sp_client_id]
        if args.sp_certificate:
            common += ["--sp-certificate", args.sp_certificate]
        elif args.sp_client_secret:
            common += ["--sp-client-secret", args.sp_client_secret]

    # Default date window: first day of previous month → today (UTC)
    today = datetime.now(timezone.utc).date()
    first_of_this_month = today.replace(day=1)
    default_from = (first_of_this_month - timedelta(days=1)).replace(day=1).isoformat()
    default_to = today.isoformat()

    effective_from = args.date_from or default_from
    effective_to = args.date_to or default_to
    date_args: list[str] = ["--from", effective_from, "--to", effective_to]
    print(f"  Date range  : {effective_from} → {effective_to}")

    # Optional lookback override for metrics scripts only
    lookback_args: list[str] = []
    if args.lookback and not args.date_from and not args.date_to:
        lookback_args = ["--lookback", args.lookback]

    commitments_extra: list[str] = ["--no-utilisation"] if args.no_utilisation else []

    # ── Script registry: (stem, display_label, extra_args) ───────────────────
    script_registry: list[tuple[str, str, list[str]]] = [
        ("get_subscriptions",      "Subscriptions",        []),
        ("get_daily_costs",        "Daily Costs",          date_args),
        ("get_reservations_commitments", "Reservations Commitments", commitments_extra),
        ("get_quota_consumption", "Quota Consumption", []),
        ("get_virtualmachines",    "Virtual Machines",      lookback_args if lookback_args else date_args),
        ("get_containerApps",      "Container Apps",        lookback_args if lookback_args else date_args),
        ("get_appserviceplans",    "App Service Plans",     lookback_args if lookback_args else date_args),
        ("get_storage_accounts",   "Storage Accounts",      []),
        ("get_keyvaults",          "Key Vaults",            []),
        ("get_app_secrets_expiry", "Entra ID App Registrations", []),
        ("get_postgresql",         "PostgreSQL",            lookback_args if lookback_args else date_args),
        ("get_sql",                "Azure SQL",             lookback_args if lookback_args else date_args),
        ("get_eventhubnamespaces", "Event Hub Namespaces",  lookback_args if lookback_args else date_args),
        ("get_loganalyticsworkspace", "Log Analytics Workspaces", lookback_args if lookback_args else date_args),
    ]

    # ── Apply --only / --skip filters ─────────────────────────────────────────
    # Backward compatibility: accept legacy script stem aliases in --only/--skip.
    script_aliases = {
        "check_app_secrets_expiry": "get_app_secrets_expiry",
    }

    only_set = {
        script_aliases.get(s.lower().replace(".py", ""), s.lower().replace(".py", ""))
        for s in args.only
    }
    skip_set = {
        script_aliases.get(s.lower().replace(".py", ""), s.lower().replace(".py", ""))
        for s in args.skip
    }

    selected_scripts = []
    for stem, label, extra in script_registry:
        stem_lower = stem.lower()
        if only_set and stem_lower not in only_set:
            continue
        if stem_lower in skip_set:
            continue
        selected_scripts.append((stem, label, extra))

    _banner("Dependency Preflight")
    preflight_ok, preflight_report = run_dependency_preflight(selected_scripts)
    if preflight_ok:
        _print_success(f"  ✓  Preflight passed for {len(selected_scripts)} script(s)")
    else:
        print_dependency_failure_report(preflight_report)
        sys.exit(1)

    # ── Execute extraction scripts ────────────────────────────────────────────
    results: dict[str, str] = {}

    for stem, label, extra in script_registry:
        stem_lower = stem.lower()

        if only_set and stem_lower not in only_set:
            _print_warning(f"\n  ⏭  Skipping {stem}.py  (not in --only list)")
            results[stem] = "skipped"
            continue

        if stem_lower in skip_set:
            _print_warning(f"\n  ⏭  Skipping {stem}.py  (--skip)")
            results[stem] = "skipped"
            continue

        script_path = EXTRACTOR_DIR / f"{stem}.py"
        if not script_path.exists():
            _print_warning(f"\n  ⚠  Script not found: {script_path} — skipping")
            results[stem] = "not found"
            continue

        if stem == "get_subscriptions":
            subscriptions_args = [
                "--output-dir", str(out_dir),
                "--output-format", args.output_format,
                "--input", str(customer_json_path),
            ]
            if args.sp_client_id:
                subscriptions_args += ["--sp-client-id", args.sp_client_id]
                if args.sp_certificate:
                    subscriptions_args += ["--sp-certificate", args.sp_certificate]
                elif args.sp_client_secret:
                    subscriptions_args += ["--sp-client-secret", args.sp_client_secret]
            success = run_subscriptions_script(script_path, tenant_ids, subscriptions_args, label)
        else:
            success = run_script(script_path, common + extra, label)
        results[stem] = "ok" if success else "failed"

    # ── Summary ───────────────────────────────────────────────────────────────
    W = 70
    print(f"\n{'═' * W}")
    print(f"  Managed Service Wrapper — Summary")
    print(f"{'─' * W}")
    for stem, status in results.items():
        icon = {"ok": "✓", "skipped": "⏭", "failed": "✗", "not found": "?"}.get(status, " ")
        print(f"  {icon}  {stem:<35} {status}")
    print(f"{'─' * W}")
    print(f"  Output: {out_dir}")
    print(f"{'═' * W}\n")

    _log_file.close()
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__

    # ── Resolve blob prefix once (shared by manifest download and upload) ─────
    blob_prefix = args.storage_prefix or out_dir.name if args.storage_connection_string else None

    # ── Manifest generation ───────────────────────────────────────────────────
    _banner("Generating Manifest")
    existing_manifest: dict = {}

    if args.storage_connection_string and blob_prefix is not None:
        print(f"  Checking for existing manifest in blob storage…")
        existing_manifest = _download_manifest_from_blob(
            args.storage_connection_string,
            args.storage_container,
            blob_prefix,
        )
        if existing_manifest:
            _print_success(
                f"  ✓  Downloaded existing manifest "
                f"(customer: {existing_manifest.get('customer', '?')}, "
                f"dashboards: {len(existing_manifest.get('dashboards', []))})"
            )
        else:
            print("  ℹ  No existing manifest in storage — creating fresh manifest")

    try:
        build_manifest, _ = _get_manifest_builder()
        manifest = build_manifest(out_dir, existing_manifest)
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        _print_success(
            f"  ✓  manifest.json written "
            f"({len(manifest.get('dashboards', []))} dashboard(s), "
            f"{len(manifest.get('other_files', []))} other file(s))"
        )
    except Exception as exc:
        _print_warning(f"  ⚠  Manifest generation failed; continuing without it: {exc}")

    # ── Blob upload (manifest-referenced files) ───────────────────────────────
    if args.storage_connection_string and blob_prefix is not None:
        _banner("Blob Upload")
        print(f"  Container  : {args.storage_container}")
        print(f"  Prefix     : {blob_prefix}")
        try:
            success = run_manifest_upload_script(
                report_dir=out_dir,
                connection_input=args.storage_connection_string,
                container_name=args.storage_container,
                blob_prefix=blob_prefix,
            )
            if success:
                _print_success("  ✓  Manifest upload stage completed")
            else:
                _print_warning("  ⚠  Manifest upload stage failed")
        except Exception as exc:
            _print_warning(f"  ⚠  Blob upload failed; local files are preserved: {exc}")

    # ── QC document generation (optional) ──────────────────────────────────
    if args.fill_qc:
        _banner("Generating QC Document")
        fill_qc_script = SCRIPT_DIR / "fill_qc_template.py"
        if not fill_qc_script.exists():
            _print_warning(f"  ⚠  fill_qc_template.py not found: {fill_qc_script}")
        else:
            fill_qc_args = [
                "-c", customer,
                "--reports-dir", str(output_root),
            ]
            if args.qc_template:
                fill_qc_args += ["--template", args.qc_template]
            if args.qc_template_version:
                fill_qc_args += ["--template-version", args.qc_template_version]
            if args.qc_output:
                fill_qc_args += ["--output", args.qc_output]
            if args.storage_connection_string and blob_prefix is not None:
                fill_qc_args += [
                    "--storage-connection-string", args.storage_connection_string,
                    "--storage-container", args.storage_container,
                    "--storage-prefix", blob_prefix,
                ]
            qc_ok = run_script(fill_qc_script, fill_qc_args, "QC Template")
            if not qc_ok:
                _print_warning("  ⚠  QC document generation failed (extraction results are still available)")

    if any(r == "failed" for r in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
