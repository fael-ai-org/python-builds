#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Iterable


TAG_PATTERN = re.compile(r"^v(?P<major>\d+\.\d+)\.(?P<patch>\d+)$")


def fetch_tags() -> list[str]:
    tags: list[str] = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/python/cpython/tags?per_page=100&page={page}"
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "python-builds-managed/1.0",
        }
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not payload:
            break
        tags.extend(item["name"] for item in payload if "name" in item)
        page += 1
    return tags


def latest_for_major(tags: Iterable[str], major: str) -> str:
    best_patch: int | None = None
    for tag in tags:
        match = TAG_PATTERN.match(tag)
        if not match:
            continue
        if match.group("major") != major:
            continue
        patch = int(match.group("patch"))
        if best_patch is None or patch > best_patch:
            best_patch = patch
    if best_patch is None:
        raise RuntimeError(f"Could not resolve any patch version for major {major}")
    return f"{major}.{best_patch}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve latest CPython patch versions for major lines."
    )
    parser.add_argument(
        "--major",
        action="append",
        default=[],
        help="Major version line (example: 3.12). May be provided multiple times.",
    )
    parser.add_argument(
        "--majors-file",
        help="Path to JSON file with {'majors': ['3.12', ...]}",
    )
    args = parser.parse_args()

    majors = list(args.major)
    if args.majors_file:
        payload = json.loads(Path(args.majors_file).read_text(encoding="utf-8"))
        majors.extend(payload.get("majors", []))
    majors = sorted(set(majors))
    if not majors:
        raise RuntimeError("No majors provided. Use --major and/or --majors-file.")

    tags = fetch_tags()
    result = {major: latest_for_major(tags, major) for major in majors}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
