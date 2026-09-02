#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


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
    kept: list[str] = []
    for asset in sorted(source_dir.iterdir()):
        if not asset.is_file():
            continue
        name = asset.name
        if not should_keep_previous_asset(name, current_versions):
            print(f"Dropping superseded {name}.")
            continue
        destination = dest_dir / name
        if destination.exists():
            print(f"Replacing previous {name} with newly built artifact.")
            continue
        print(f"Keeping {name}.")
        shutil.copy2(asset, destination)
        kept.append(name)
    return kept


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy only the current latest patch per major from a previous python-builds release."
    )
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--dest-dir", required=True)
    args = parser.parse_args()
    plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    carry_forward_assets(
        source_dir=Path(args.source_dir),
        dest_dir=Path(args.dest_dir),
        plan=plan,
    )


if __name__ == "__main__":
    main()
