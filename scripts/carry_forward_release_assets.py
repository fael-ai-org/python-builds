#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from release_inventory import expected_archives


_ASSET_VERSION_RE = re.compile(r"^python-(?P<version>\d+\.\d+\.\d+)-")


def current_release_versions(plan: dict) -> set[str]:
    versions = plan.get("current_versions") or {}
    return {str(value) for value in versions.values() if str(value).strip()}


def should_keep_previous_asset(name: str, current_versions: set[str]) -> bool:
    match = _ASSET_VERSION_RE.match(name)
    if match is None:
        return False
    return match.group("version") in current_versions


def carry_forward_assets(*, source_dir: Path, dest_dir: Path, plan: dict) -> list[str]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    current_versions = current_release_versions(plan)
    archives = expected_archives(plan["current_versions"])
    allowed = archives | {name + ".sha256" for name in archives}
    kept: list[str] = []
    for asset in sorted(source_dir.iterdir()):
        if not asset.is_file():
            continue
        name = asset.name
        if name not in allowed or not should_keep_previous_asset(name, current_versions):
            print(f"Dropping superseded {name}.")
            continue
        destination = dest_dir / name
        if destination.exists():
            print(f"Replacing previous {name} with newly built artifact.")
            continue
        if plan.get("inventory_checked"):
            expected = plan["carry_forward_digests"].get(name)
            with asset.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if expected != actual:
                raise ValueError("Carried asset differs from inspected release: " + name)
        print(f"Keeping {name}.")
        shutil.copy2(asset, destination)
        kept.append(name)
    return kept


def download_carried_assets(plan: dict, root: Path, repository: str) -> None:
    # Download only missing required files, using the IDs and digests inspected
    # by the planner. New builds win; a missing previous release is harmless
    # only when every required file was rebuilt.
    from release_asset_download import open_https
    if repository != "fael-ai-org/python-builds":
        raise ValueError("Release repository is not approved")
    expected = expected_archives(plan["current_versions"])
    required = expected | {name + ".sha256" for name in expected}
    sources = {a["name"]: a for a in plan["carry_forward_sources"]}
    root.mkdir(parents=True, exist_ok=True)
    for name in sorted(required):
        destination = root / name
        if destination.exists():
            continue
        asset = sources.get(name)
        if (not asset or type(asset.get("id")) is not int or asset["id"] <= 0
                or type(asset.get("size")) is not int or not 0 < asset["size"] <= 2 * 1024**3
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or "")):
            raise ValueError("Missing verified carry-forward source: " + name)
        digest = hashlib.sha256()
        count = 0
        with open_https(f"https://api.github.com/repos/{repository}/releases/assets/{asset['id']}") as incoming:
            with destination.open("xb") as output:
                while chunk := incoming.read(1024 * 1024):
                    count += len(chunk)
                    if count > asset["size"]:
                        raise ValueError("Carried asset exceeds its declared size")
                    digest.update(chunk)
                    output.write(chunk)
        if count != asset["size"] or "sha256:" + digest.hexdigest() != asset["digest"]:
            raise ValueError("Carried asset differs from inspected release: " + name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy only the current latest patch per major from a previous python-builds release."
    )
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--source-dir")
    parser.add_argument("--download", action="store_true", help="Fetch only missing required files through the GitHub API.")
    parser.add_argument("--dest-dir", required=True)
    args = parser.parse_args()
    plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    if args.download:
        download_carried_assets(plan, Path(args.dest_dir), os.environ["GITHUB_REPOSITORY"])
        return
    if not args.source_dir:
        parser.error("--source-dir is required unless --download is selected")
    carry_forward_assets(
        source_dir=Path(args.source_dir),
        dest_dir=Path(args.dest_dir),
        plan=plan,
    )


if __name__ == "__main__":
    main()
