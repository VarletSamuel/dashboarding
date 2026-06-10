#!/usr/bin/env python3
"""Delete files in a folder that are not referenced by a manifest.json file.

Safety behavior:
- Dry-run by default (only prints files that would be deleted).
- Requires --delete to actually remove files.
- Keeps manifest.json by default.

Examples:
    python delete_unreferenced_manifest_files.py ../reports/CUST/manifest.json
    python delete_unreferenced_manifest_files.py ../reports/CUST/manifest.json --delete
    python delete_unreferenced_manifest_files.py ../reports/CUST/manifest.json --recursive --delete
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, List, Set


def _load_manifest(manifest_path: Path) -> Dict:
    if not manifest_path.exists() or not manifest_path.is_file():
        raise FileNotFoundError(f"Manifest file not found: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _normalize_rel_path(value: str) -> Path:
    # Manifest entries may contain either slash style.
    normalized = value.replace("\\", "/")
    return Path(normalized)


def _collect_manifest_paths(manifest: Dict) -> Set[Path]:
    referenced: Set[Path] = set()

    for dashboard in manifest.get("dashboards", []):
        for file_spec in dashboard.get("files", []):
            filename = file_spec.get("filename")
            if isinstance(filename, str) and filename.strip():
                referenced.add(_normalize_rel_path(filename.strip()))

    for other in manifest.get("other_files", []):
        filename = other.get("filename")
        if isinstance(filename, str) and filename.strip():
            referenced.add(_normalize_rel_path(filename.strip()))

    return referenced


def _list_candidate_files(root_dir: Path, recursive: bool) -> Iterable[Path]:
    if recursive:
        yield from (path for path in root_dir.rglob("*") if path.is_file())
    else:
        yield from (path for path in root_dir.iterdir() if path.is_file())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete files not referenced by manifest.json (dry-run by default).",
        add_help=False,
    )
    parser.add_argument("-h", "--help", "-?", action="help", help="Show this help message and exit.")
    parser.add_argument(
        "manifest_path",
        help="Path to manifest.json file.",
    )
    parser.add_argument(
        "--root-dir",
        default=None,
        help="Folder to clean. Defaults to the folder containing manifest.json.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Include files in subfolders under root-dir.",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Actually delete files. Without this flag, script only prints what would be deleted.",
    )
    parser.add_argument(
        "--delete-manifest",
        action="store_true",
        help="Allow deleting manifest.json if it is not referenced (disabled by default).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    manifest_path = Path(args.manifest_path).resolve()
    root_dir = Path(args.root_dir).resolve() if args.root_dir else manifest_path.parent.resolve()

    if not root_dir.exists() or not root_dir.is_dir():
        print(f"Error: root directory does not exist or is not a directory: {root_dir}")
        return 1

    try:
        manifest = _load_manifest(manifest_path)
    except (OSError, json.JSONDecodeError, FileNotFoundError) as exc:
        print(f"Error: could not load manifest: {exc}")
        return 1

    referenced_rel_paths = _collect_manifest_paths(manifest)

    referenced_abs_paths: Set[Path] = set()
    for rel_path in referenced_rel_paths:
        if rel_path.is_absolute():
            referenced_abs_paths.add(rel_path.resolve())
        else:
            referenced_abs_paths.add((root_dir / rel_path).resolve())

    if not args.delete_manifest:
        referenced_abs_paths.add(manifest_path)

    candidates = list(_list_candidate_files(root_dir=root_dir, recursive=args.recursive))
    unreferenced = [path for path in candidates if path.resolve() not in referenced_abs_paths]
    unreferenced.sort(key=lambda p: str(p).lower())

    mode = "DELETE" if args.delete else "DRY-RUN"
    print(f"Mode: {mode}")
    print(f"Manifest: {manifest_path}")
    print(f"Root dir: {root_dir}")
    print(f"Referenced files: {len(referenced_abs_paths)}")
    print(f"Candidate files scanned: {len(candidates)}")
    print(f"Unreferenced files found: {len(unreferenced)}")

    if not unreferenced:
        print("Nothing to delete.")
        return 0

    for file_path in unreferenced:
        rel = file_path.relative_to(root_dir)
        print(f"  - {rel.as_posix()}")

    if not args.delete:
        print("\nDry-run complete. Re-run with --delete to remove these files.")
        return 0

    deleted = 0
    failed = 0
    for file_path in unreferenced:
        try:
            file_path.unlink()
            deleted += 1
        except OSError as exc:
            failed += 1
            print(f"Failed to delete {file_path}: {exc}")

    print(f"\nDeleted: {deleted}")
    print(f"Failed: {failed}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
