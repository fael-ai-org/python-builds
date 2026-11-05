"""Configured release targets, remote inventory and final byte-level completeness gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


TARGETS = (
    {"runner": "ubuntu-24.04", "target_os": "linux", "target_arch": "x86_64", "archive_ext": ".tar.gz"},
    {"runner": "windows-2022", "target_os": "windows", "target_arch": "x86_64", "archive_ext": ".zip"},
    {"runner": "macos-15-intel", "target_os": "macos", "target_arch": "x86_64", "archive_ext": ".tar.gz"},
    {"runner": "macos-15", "target_os": "macos", "target_arch": "arm64", "archive_ext": ".tar.gz"},
)


def archive_name(version: str, target: dict) -> str:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Invalid Python version")
    return f"python-{version}-{target['target_os']}-{target['target_arch']}{target['archive_ext']}"


def expected_archives(versions: dict) -> set[str]:
    return {archive_name(version, target) for version in versions.values() for target in TARGETS}


def valid_asset(asset: dict) -> bool:
    return (asset.get("state") == "uploaded" and type(asset.get("size")) is int
            and asset["size"] > 0
            and re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest") or "") is not None)


def build_target(major: str, detail: dict, target: dict) -> dict:
    name = archive_name(detail["version"], target)
    return {"major": major, "python_version": detail["version"],
            "cpython_tag": detail.get("tag", "v" + detail["version"]),
            "cpython_tag_commit_sha": detail.get("tag_commit_sha", ""),
            "artifact_name": name.removesuffix(target["archive_ext"]), "archive_name": name, **target}


def available_archives(inventory: dict) -> set[str]:
    assets = inventory.get("assets", [])
    names = [a["name"] for a in assets]
    if len(names) != len(set(names)):
        raise ValueError("Ambiguous release inventory")
    valid = {a["name"] for a in assets if valid_asset(a)}
    return {name for name in valid if name + ".sha256" in valid}


def fetch_inventory(tag: str, repository: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    def get(path):
        request = Request("https://api.github.com/repos/" + repository + path,
                          headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"],
                                   "Accept": "application/vnd.github+json",
                                   "User-Agent": "portable-python-inventory"})
        try:
            with urlopen(request, timeout=60) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            if status == 404 and path.startswith("/releases/tags/"):
                return None
            raise RuntimeError(f"Release inventory request failed (HTTP {status})") from None
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Release inventory exceeds its limit")
        return json.loads(raw)
    if not tag:
        return {"assets": []}
    release = get("/releases/tags/" + quote(tag, safe=""))
    if release is None:
        return {"assets": []}
    if release.get("tag_name") != tag or type(release.get("id")) is not int:
        raise ValueError("Release inventory identity mismatch")
    assets = []
    for page in range(1, 101):
        batch = get(f"/releases/{release['id']}/assets?per_page=100&page={page}")
        if not isinstance(batch, list):
            raise ValueError("Invalid release inventory")
        assets.extend(batch)
        if len(batch) < 100:
            return {"id": release["id"], "tag_name": tag, "draft": release["draft"],
                    "immutable": release.get("immutable", False), "assets": assets}
    raise ValueError("Release inventory exceeds its page limit")


def validate_directory(root: Path, versions: dict) -> list[str]:
    expected = expected_archives(versions)
    allowed = expected | {name + ".sha256" for name in expected} | {"SHA256SUMS.txt"}
    actual = {path.name for path in root.iterdir()}
    required = allowed - {"SHA256SUMS.txt"}
    if required - actual or actual - allowed:
        raise ValueError(f"Incomplete release: missing={sorted(required - actual)}, unexpected={sorted(actual - allowed)}")
    sums = []
    for name in sorted(expected):
        archive, sidecar = root / name, root / (name + ".sha256")
        if (archive.is_symlink() or sidecar.is_symlink() or not archive.is_file()
                or not sidecar.is_file() or archive.stat().st_size == 0):
            raise ValueError("Release requires nonempty regular archives and checksum files")
        if sidecar.stat().st_size > 512:
            raise ValueError("Invalid checksum file")
        text = sidecar.read_text(encoding="utf-8").strip()
        match = re.fullmatch(r"([0-9a-f]{64}) [ *]" + re.escape(name), text)
        if match is None:
            raise ValueError("Checksum filename or format mismatch: " + name)
        with archive.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != match[1]:
            raise ValueError("Archive checksum mismatch: " + name)
        sums.append(f"{digest}  {name}\n")
    return sums


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--state-file", required=True)
    fetch.add_argument("--output", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--plan-file", required=True)
    verify.add_argument("--directory", required=True)
    guard = sub.add_parser("guard-target")
    guard.add_argument("--plan-file", required=True)
    args = parser.parse_args()
    if args.mode == "fetch":
        state = json.loads(Path(args.state_file).read_text()) if Path(args.state_file).exists() else {}
        inventory = fetch_inventory(state.get("release_tag", ""), os.environ["GITHUB_REPOSITORY"])
        Path(args.output).write_text(json.dumps(inventory) + "\n")
    elif args.mode == "guard-target":
        plan = json.loads(Path(args.plan_file).read_text())
        inventory = fetch_inventory(plan["release_tag"], os.environ["GITHUB_REPOSITORY"])
        if inventory.get("id") and (not inventory["draft"] or inventory.get("immutable")
                                    or any(a["name"].startswith("release-signing.") for a in inventory["assets"])):
            raise ValueError("Refusing to replace a published, immutable or already signed release")
    else:
        plan = json.loads(Path(args.plan_file).read_text())
        root = Path(args.directory)
        sums = validate_directory(root, plan["current_versions"])
        (root / "SHA256SUMS.txt").write_text("".join(sums), encoding="utf-8")
        print(f"Verified complete release: {len(sums)} archives and matching checksums")


if __name__ == "__main__":
    main()
