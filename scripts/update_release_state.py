#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def safe_snapshot_name(tag: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", tag)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write committed release-state snapshots for an automated release."
    )
    parser.add_argument("--plan-file", required=True, help="Path to a release plan JSON file.")
    parser.add_argument(
        "--output-dir",
        default="release-state",
        help="Directory where latest.json and history snapshots are written.",
    )
    parser.add_argument("--build-source-commit", help="Source commit used for the build run.")
    parser.add_argument("--workflow-run-id", help="GitHub Actions workflow run id.")
    parser.add_argument("--workflow-run-url", help="GitHub Actions workflow run URL.")
    parser.add_argument("--repository", help="Repository name in owner/repo form.")
    parser.add_argument("--event-name", help="GitHub Actions event name.")
    args = parser.parse_args()

    plan = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    if not plan.get("should_build"):
        raise RuntimeError("Refusing to write release state for a plan with should_build=false")

    output_dir = Path(args.output_dir)
    history_dir = output_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "schema_version": 1,
        "release_tag": plan["release_tag"],
        "release_reason": plan["release_reason"],
        "released_at_utc": datetime.now(timezone.utc).isoformat(),
        "versions": plan["current_versions"],
        "details": plan["current_details"],
        "changed_versions": plan["changed_versions"],
        "changed_details": plan["changed_details"],
        "changed_majors": plan["changed_majors"],
        "build_source_commit": args.build_source_commit,
        "workflow_run": {
            "id": args.workflow_run_id,
            "url": args.workflow_run_url,
            "repository": args.repository,
            "event_name": args.event_name,
        },
    }

    latest_path = output_dir / "latest.json"
    history_path = history_dir / f"{safe_snapshot_name(plan['release_tag'])}.json"

    latest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    history_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "latest_path": str(latest_path),
                "history_path": str(history_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()