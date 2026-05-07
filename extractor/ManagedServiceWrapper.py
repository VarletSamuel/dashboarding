#!/usr/bin/env python3
"""
ManagedServiceWrapper.py
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
    3. get_reserved_instances.py
    4. get_virtualmachines.py
    5. get_containerApps.py
    6. get_appserviceplans.py
    7. get_eventhubnamespaces.py

Output is written to <output-dir>/<CUSTOMER>_<YYYYMMDD_HHMM>/ so each run
gets its own timestamped sub-folder.

Usage
-----
    python ManagedServiceWrapper.py -c CUST
    python ManagedServiceWrapper.py -c CUST -i ../customers/CUST.json --output-dir ../reports
    python ManagedServiceWrapper.py -c CUST --from 2026-02-01 --to 2026-04-20
    python ManagedServiceWrapper.py -c CUST --lookback PT6H
    python ManagedServiceWrapper.py -c CUST --skip get_subscriptions.py get_daily_costs.py get_reserved_instances.py get_virtualmachines.py get_containerApps.py get_appserviceplans.py get_eventhubnamespaces.py
    python ManagedServiceWrapper.py -c CUST --skip-login
    python ManagedServiceWrapper.py -c CUST --sp-client-id <appId> --sp-client-secret <secret>
    python ManagedServiceWrapper.py -c CUST --sp-client-id <appId> --sp-certificate /path/to/cert.pem
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

# On Windows the Azure CLI wrapper is az.cmd, not az
AZ_CMD = "az.cmd" if sys.platform == "win32" else "az"

# All scripts live next to this wrapper
SCRIPT_DIR = Path(__file__).parent
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


def run_script(script_path: Path, script_args: list[str], label: str) -> bool:
    """Run *script_path* as a subprocess; return True on exit code 0."""
    cmd = [sys.executable, str(script_path)] + script_args
    _banner(f"Running: {label}", char="─")
    print(f"  $ {' '.join(cmd)}\n")
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
    if result.returncode != 0:
        _print_error(f"\n  ✗  {label} exited with code {result.returncode}")
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
        if result.returncode != 0:
            _print_error(f"\n  ✗  {label} failed for tenant {tenant_id} with code {result.returncode}")
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
        script_path = SCRIPT_DIR / f"{stem}.py"
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
    usage="""%(prog)s\n  -c CUSTOMER\n  [-i INPUT]\n  [--output-dir OUTPUT_DIR]\n  [--output-format {csv,json,both}]\n  [--skip-login]\n  [--sp-client-id APP_ID]\n  [--sp-client-secret SECRET]\n  [--sp-certificate CERT_PATH]\n  [--from DATE_FROM]\n  [--to DATE_TO]\n  [--lookback LOOKBACK]\n  [--no-utilisation]\n  [--skip SCRIPT [SCRIPT ...]]\n  [--only SCRIPT [SCRIPT ...]]""",
        epilog="""
examples:
  %(prog)s -c CUST
        %(prog)s -c CUST -i ../customers/CUST.json --output-dir ./reports
  %(prog)s -c CUST --from 2026-02-01 --to 2026-04-20
  %(prog)s -c CUST --lookback PT6H
      %(prog)s -c CUST --skip get_subscriptions get_reserved_instances get_eventhubnamespaces
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
        help="Root output directory. A sub-folder <CUSTOMER>_<timestamp> is created per run "
               "(default: ../reports relative to this script).",
    )
    parser.add_argument(
        "--output-format",
        choices=("csv", "json", "both"),
        default="both",
        help="File output format for child extractors: csv, json, or both (default: both).",
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
               "get_eventhubnamespaces, get_containerApps, get_appserviceplans). "
               "Default: first day of previous month.",
    )
    parser.add_argument(
        "--to", dest="date_to", default=None,
           help="End date YYYY-MM-DD (forwarded to get_daily_costs, "
               "get_eventhubnamespaces, get_containerApps, get_appserviceplans). "
               "Default: today.",
    )
    parser.add_argument(
        "--lookback",
        default=None,
        help="Metrics lookback window: integer minutes or ISO-8601 duration like PT6H. "
                         "Forwarded to get_eventhubnamespaces, get_containerApps, and get_appserviceplans. "
               "Overrides the default date window for those two scripts when --from/--to are not set.",
    )

    # ── Reserved Instances ────────────────────────────────────────────────────
    parser.add_argument(
        "--no-utilisation",
        action="store_true",
        help="Skip RI utilisation fetch in get_reserved_instances (faster run).",
    )

    # ── Selective execution ───────────────────────────────────────────────────
    parser.add_argument(
        "--skip",
        nargs="+",
        metavar="SCRIPT",
        default=[],
        help="One or more script names to skip (without .py extension). "
               "Choices: get_subscriptions  get_daily_costs  get_reserved_instances  "
               "get_virtualmachines  get_containerApps  get_appserviceplans  get_eventhubnamespaces",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="SCRIPT",
        default=[],
        help="Run ONLY the listed script(s) — overrides --skip.",
    )

    return parser


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    customer = args.customer.upper()
    customer_csv_path = resolve_customer_csv(customer, args.input)
    output_root = Path(args.output_dir) if args.output_dir else (SCRIPT_DIR.parent / "reports")

    # ── Timestamped output directory ──────────────────────────────────────────
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    out_dir = output_root / f"{customer}_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Tee stdout + stderr to a log file ─────────────────────────────────────
    log_path = out_dir / f"{customer}_log.txt"
    _log_file = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, _log_file)
    sys.stderr = _Tee(sys.__stderr__, _log_file)

    _banner("Managed Service Wrapper")
    print(f"  Customer    : {customer}")
    print(f"  Input file  : {customer_csv_path}")
    print(f"  Output dir  : {out_dir}")
    print(f"  Run date    : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    # ── Auto-read AppReg client_id from JSON if not given on CLI ──────────────
    customer_data = read_customer_json(customer_csv_path)
    json_client_id = (customer_data.get("authentication") or {}).get("client_id", "")
    if json_client_id and not args.sp_client_id and (args.sp_client_secret or args.sp_certificate):
        args.sp_client_id = json_client_id
        print(f"  App Reg     : {json_client_id} (from customer JSON)")
    elif json_client_id and not args.sp_client_id:
        print(f"  App Reg     : {json_client_id} (available in customer JSON, not used without secret/certificate)")

    tenant_ids = read_tenants(customer_csv_path)

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
        "-i", str(customer_csv_path),
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

    ri_extra: list[str] = ["--no-utilisation"] if args.no_utilisation else []

    # ── Script registry: (stem, display_label, extra_args) ───────────────────
    script_registry: list[tuple[str, str, list[str]]] = [
        ("get_subscriptions",      "Subscriptions",        []),
        ("get_daily_costs",        "Daily Costs",          date_args),
        ("get_reserved_instances", "Reserved Instances",    ri_extra),
        ("get_virtualmachines",    "Virtual Machines",      lookback_args if lookback_args else date_args),
        ("get_containerApps",      "Container Apps",        lookback_args if lookback_args else date_args),
        ("get_appserviceplans",    "App Service Plans",     lookback_args if lookback_args else date_args),
        ("get_eventhubnamespaces", "Event Hub Namespaces",  lookback_args if lookback_args else date_args),
    ]

    # ── Apply --only / --skip filters ─────────────────────────────────────────
    only_set  = {s.lower().replace(".py", "") for s in args.only}
    skip_set  = {s.lower().replace(".py", "") for s in args.skip}

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

        script_path = SCRIPT_DIR / f"{stem}.py"
        if not script_path.exists():
            _print_warning(f"\n  ⚠  Script not found: {script_path} — skipping")
            results[stem] = "not found"
            continue

        if stem == "get_subscriptions":
            subscriptions_args = ["--output-dir", str(out_dir), "--output-format", args.output_format]
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

    if any(r == "failed" for r in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
