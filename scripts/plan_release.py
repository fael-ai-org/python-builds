#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path


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
    if has_actual_changes:
        version_slug = "-".join(current_versions[major] for major in sorted(current_versions))
        return f"python-{version_slug}"

    if force:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%SZ")
        return f"python-force-{timestamp}"

    return ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan which Python versions need to be built and released."
    )
    parser.add_argument("--resolved-file", required=True, help="Path to resolved details JSON.")
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

    build_details = changed_details
    build_versions = changed_versions
    if args.force and not build_details:
        build_details = current_details
        build_versions = current_versions

    should_build = bool(build_details)
    release_tag = args.release_tag or default_release_tag(current_versions, has_actual_changes, args.force)

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
        "release_tag": release_tag,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()