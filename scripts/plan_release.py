#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from release_inventory import TARGETS, archive_name, available_archives, build_target, expected_archives, valid_asset


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_previous_details(payload: dict | None) -> dict[str, dict[str, str]]:
    if not payload:
        return {}

    details = payload.get("details")
    if isinstance(details, dict):
        return details

    if all(isinstance(value, str) for value in payload.values()):
        return {
            major: {
                "version": value,
                "tag": f"v{value}",
                "tag_commit_sha": "",
            }
            for major, value in payload.items()
        }

    return {}


def versions_from_details(details: dict[str, dict[str, str]]) -> dict[str, str]:
    return {
        major: data["version"]
        for major, data in sorted(details.items())
    }


def default_release_tag(
    current_versions: dict[str, str],
    has_actual_changes: bool,
    force: bool,
) -> str:
    if has_actual_changes or force:
        version_slug = "-".join(current_versions[major] for major in sorted(current_versions))
        return f"python-{version_slug}"

    return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan which Python versions need to be built and released."
    )
    parser.add_argument("--resolved-file", required=True, help="Path to resolved details JSON.")
    parser.add_argument("--inventory-file", help="Live inventory of the state release; required in CI.")
    parser.add_argument("--candidate-suffix", help="Unique build ID for a fresh candidate when the state release is public.")
    parser.add_argument(
        "--state-file",
        default="release-state/latest.json",
        help="Path to the committed release state file.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force a rebuild even if no versions changed.",
    )
    parser.add_argument(
        "--release-tag",
        help="Optional explicit release tag override.",
    )
    args = parser.parse_args()

    current_details = load_json(Path(args.resolved_file))
    current_versions = versions_from_details(current_details)

    state_path = Path(args.state_file)
    previous_payload = load_json(state_path) if state_path.exists() else None
    previous_details = extract_previous_details(previous_payload)
    previous_versions = versions_from_details(previous_details)

    changed_details = {
        major: current_details[major]
        for major in sorted(current_details)
        if previous_versions.get(major) != current_details[major]["version"]
    }
    changed_versions = versions_from_details(changed_details)
    has_actual_changes = bool(changed_details)

    release_reason = "no-change"
    if not previous_details:
        release_reason = "initial-release"
    elif changed_details:
        release_reason = "version-bump"
    elif args.force:
        release_reason = "forced-rebuild"

    if args.force:
        build_details = current_details
        build_versions = current_versions
    else:
        build_details = changed_details
        build_versions = changed_versions

    inventory = load_json(Path(args.inventory_file)) if args.inventory_file else None
    available = available_archives(inventory) if inventory is not None else None
    if inventory and inventory.get("tag_name") and inventory["tag_name"] != (previous_payload or {}).get("release_tag"):
        raise ValueError("Inventory does not match the recorded release")
    build_targets = []
    for major, detail in sorted(current_details.items()):
        for target in TARGETS:
            name = archive_name(detail["version"], target)
            if major in build_details or (available is not None and name not in available):
                build_targets.append(build_target(major, detail, target))
    # The release-level checksum index is required too. Rebuild one target to
    # enter the existing build/validation/release path, then regenerate the
    # complete index from verified bytes without weakening those dependencies.
    if current_details and inventory is not None and not build_targets:
        indexes = [a for a in inventory.get("assets", []) if a["name"] == "SHA256SUMS.txt"]
        if not indexes or not valid_asset(indexes[0]):
            major = sorted(current_details)[0]
            detail, target = current_details[major], TARGETS[0]
            build_targets.append(build_target(major, detail, target))
    build_details = {major: current_details[major] for major in sorted({t["major"] for t in build_targets})}
    build_versions = versions_from_details(build_details)
    should_build = bool(build_targets)
    if should_build and not has_actual_changes and not args.force:
        release_reason = "missing-assets"
    release_tag = args.release_tag or default_release_tag(current_versions, should_build, args.force)
    if (should_build and inventory and release_tag == inventory.get("tag_name")
            and (not inventory.get("draft") or inventory.get("immutable"))):
        import re
        if not args.candidate_suffix or not re.fullmatch(r"[0-9]+", args.candidate_suffix):
            raise ValueError("A unique candidate suffix is required; published releases are not replaced")
        release_tag += "-build-" + args.candidate_suffix

    archives = expected_archives(current_versions)
    carry_names = archives | {name + ".sha256" for name in archives}
    carry_sources = [{key: asset[key] for key in ("name", "id", "size", "digest")}
                     for asset in (inventory or {}).get("assets", [])
                     if asset["name"] in carry_names and valid_asset(asset)
                     and type(asset.get("id")) is int and asset["id"] > 0]

    result = {
        "should_build": should_build,
        "release_reason": release_reason,
        "current_versions": current_versions,
        "current_details": current_details,
        "previous_versions": previous_versions,
        "previous_details": previous_details,
        "changed_versions": build_versions,
        "changed_details": build_details,
        "changed_majors": sorted(build_versions),
        "build_targets": build_targets,
        "inventory_checked": inventory is not None,
        "carry_forward_digests": {a["name"]: a["digest"][7:] for a in carry_sources},
        "carry_forward_sources": carry_sources,
        "release_tag": release_tag,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
