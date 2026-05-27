#!/usr/bin/env python3
"""Upload manifest-referenced report files to Azure Blob Storage.

Behavior:
- Reads a manifest.json from a report folder.
- Uploads only files referenced by manifest dashboards/other_files.
- Uploads into a folder prefix inside a blob container.
- Uses a connection string that contains a SAS token.

Examples:
    python upload_manifest_reports.py ../reports/DATS --connection-string "<conn-string>"
    python upload_manifest_reports.py ../reports/DATS --connection-string "<conn-string>" --prefix DATS/2026-05
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse


def _has_sas_in_connection_string(connection_string: str) -> bool:
    parts: Dict[str, str] = {}
    for token in connection_string.split(";"):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        parts[key.strip().lower()] = value.strip()
    return bool(parts.get("sharedaccesssignature", ""))


def _is_sas_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not parsed.netloc or not parsed.path:
        return False
    query = (parsed.query or "").lower()
    return "sig=" in query


def _container_from_sas_url(sas_url: str) -> str:
    parsed = urlparse(sas_url)
    path_parts = [part for part in parsed.path.split("/") if part]
    if not path_parts:
        raise ValueError("SAS URL does not include a container path.")
    return path_parts[0]


def _load_manifest(manifest_path: Path) -> Dict:
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _collect_manifest_filenames(manifest: Dict) -> List[str]:
    files: List[str] = []

    for dashboard in manifest.get("dashboards", []):
        for file_spec in dashboard.get("files", []):
            filename = file_spec.get("filename")
            if filename:
                files.append(str(filename))

    for other in manifest.get("other_files", []):
        filename = other.get("filename")
        if filename:
            files.append(str(filename))

    # Keep first occurrence order, drop duplicates.
    deduped = list(dict.fromkeys(files))
    return deduped


def _upload_files(
    report_dir: Path,
    filenames: List[str],
    connection_input: str,
    container_name: str,
    blob_prefix: str,
    include_manifest: bool,
) -> int:
    try:
        from azure.core.exceptions import ResourceExistsError  # type: ignore[import-not-found]
        from azure.storage.blob import BlobServiceClient  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Missing Azure Blob SDK. Install with: python -m pip install azure-storage-blob"
        ) from exc

    if _is_sas_url(connection_input):
        parsed = urlparse(connection_input)
        account_url = f"{parsed.scheme}://{parsed.netloc}"
        sas_token = parsed.query
        blob_service = BlobServiceClient(account_url=account_url, credential=sas_token)
    else:
        if not _has_sas_in_connection_string(connection_input):
            raise ValueError(
                "Input must be either a SAS URL or a connection string including SharedAccessSignature."
            )
        blob_service = BlobServiceClient.from_connection_string(connection_input)

    container_client = blob_service.get_container_client(container_name)

    try:
        container_client.create_container()
    except ResourceExistsError:
        pass
    except Exception:
        # If container already exists but create permission is missing, continue.
        pass

    prefix = blob_prefix.strip("/")
    uploaded = 0

    upload_list = list(filenames)
    if include_manifest and "manifest.json" not in upload_list:
        upload_list.append("manifest.json")

    missing: List[str] = []

    for filename in upload_list:
        file_path = report_dir / filename
        if not file_path.exists() or not file_path.is_file():
            missing.append(filename)
            continue

        blob_name = f"{prefix}/{filename}" if prefix else filename
        with file_path.open("rb") as handle:
            container_client.upload_blob(name=blob_name, data=handle, overwrite=True)
        uploaded += 1

    if missing:
        print("Warning: Some manifest-referenced files were not found locally:")
        for name in missing:
            print(f"  - {name}")

    return uploaded


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload files referenced by manifest.json to Azure Blob Storage.",
        add_help=False,
    )
    parser.add_argument("-h", "--help", "-?", action="help", help="Show this help message and exit.")
    parser.add_argument(
        "report_folder",
        help="Path to report folder containing manifest.json and report files.",
    )
    parser.add_argument(
        "--connection-string",
        required=True,
        help="Either a SAS URL (container URL with token) or a connection string containing SharedAccessSignature.",
    )
    parser.add_argument(
        "--container",
        default="reports",
        help="Blob container name (default: reports).",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="Blob folder prefix inside the container (default: report folder name).",
    )
    parser.add_argument(
        "--exclude-manifest",
        action="store_true",
        help="Do not upload manifest.json (default uploads it).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    report_dir = Path(args.report_folder).resolve()
    if not report_dir.exists() or not report_dir.is_dir():
        print(f"Error: report folder does not exist or is not a directory: {report_dir}")
        return 1

    manifest_path = report_dir / "manifest.json"
    try:
        manifest = _load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"Error: could not load manifest: {exc}")
        return 1

    filenames = _collect_manifest_filenames(manifest)
    input_value = args.connection_string

    if _is_sas_url(input_value):
        sas_container = _container_from_sas_url(input_value)
        container_name = args.container if args.container else sas_container
        if container_name != sas_container:
            print(f"Warning: using --container '{container_name}' instead of SAS URL container '{sas_container}'.")
    else:
        container_name = args.container

    prefix = args.prefix if args.prefix is not None else report_dir.name

    print(f"Report folder: {report_dir}")
    print(f"Container: {container_name}")
    print(f"Prefix: {prefix}")
    print(f"Files referenced by manifest: {len(filenames)}")

    try:
        uploaded_count = _upload_files(
            report_dir=report_dir,
            filenames=filenames,
            connection_input=input_value,
            container_name=container_name,
            blob_prefix=prefix,
            include_manifest=not args.exclude_manifest,
        )
    except Exception as exc:
        print(f"Error: upload failed: {exc}")
        return 1

    print(f"Uploaded files: {uploaded_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
