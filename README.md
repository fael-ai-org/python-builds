# python-builds

Build source-based, portable CPython archives for multiple OS/architecture targets, validate the packaged standard library, and publish reproducible release artifacts through GitHub Actions.

## Overview

This repository automates a full release loop for portable Python distributions:

- Resolve latest CPython patch versions for configured major lines.
- Build per-target archives from source.
- Validate packaged interpreters with both fast and full stdlib checks.
- Publish artifacts and checksums to GitHub Releases.
- Commit release snapshots under `release-state/` for traceability.

Supported build targets:

- Windows x86_64
- Linux x86_64
- macOS x86_64
- macOS arm64

Configured major lines live in `config/majors.json`.

## Key Features

- Source builds from python.org release tarballs.
- Checksummed source download metadata recorded in each artifact `METADATA.json`.
- CPython tag and tag commit SHA tracking in packaged metadata.
- Stdlib validation gates that fail release runs on unexpected import regressions.
- Automatic release-state history snapshots for each published tag.
- Manual targeted rebuild support in GitHub Actions (major, OS, arch filters).

## Quick Start

### Build Locally

Build latest patch for a major line:

```bash
python scripts/build_portable.py 3.13
```

Build explicit version and target:

```bash
python scripts/build_portable.py \
	3.11 \
	--python-version 3.11.15 \
	--target-os macos \
	--target-arch x86_64 \
	--output-dir dist
```

### Validate a Built Archive

Run full archive validation:

```bash
python scripts/validate_distribution.py \
	dist/python-3.11.15-macos-x86_64.tar.gz \
	--baseline-python python3 \
	--full-timeout-seconds 5
```

Run checker directly inside any Python runtime:

```bash
python scripts/check_stdlib.py --mode fast
python scripts/check_stdlib.py --mode full --timeout-seconds 5
```

## GitHub Actions

Main release workflow:

- `.github/workflows/build-python.yml`

### What the Workflow Does

1. Resolves latest patch versions for configured majors.
2. Compares with `release-state/latest.json`.
3. Builds only changed majors by default.
4. Validates each built archive with fast/full checks.
5. Publishes artifacts and checksums.
6. Commits updated release-state snapshot files.
7. Creates or updates release tag/release assets.

### Manual Dispatch Inputs

- `force_rebuild`: rebuild even when no upstream patch changed.
- `release_tag`: optional release tag override.
- `major_filter`: optional single major filter (example: `3.11`).
- `target_os_filter`: `any`, `linux`, `windows`, or `macos`.
- `target_arch_filter`: `any`, `x86_64`, or `arm64`.

If any target filter is set, the workflow automatically enables force planning so you can rebuild a selected slice even when upstream versions are unchanged.

### Targeted Rebuild Example

To rebuild only CPython 3.11 for macOS Intel:

- `major_filter=3.11`
- `target_os_filter=macos`
- `target_arch_filter=x86_64`

This is the recommended path for patching one broken artifact without rebuilding every version/target.

## How It Works

### Version Resolution

- `scripts/resolve_latest_patch.py` queries `python/cpython` tags and resolves latest patch versions for each configured major.

### Release Planning

- `scripts/plan_release.py` compares resolved versions against `release-state/latest.json` and decides whether to build.

### Building

- `scripts/build_portable.py` downloads source, verifies checksum when available, builds CPython, packages archive, and writes `METADATA.json`.
- On macOS, runtime dylib dependencies are bundled to improve portability.
- CPython 3.11 on macOS prefers Homebrew `tcl-tk@8` for reliable `_tkinter` builds.

### Validation

- `scripts/check_stdlib.py` performs fast and full import checks.
- `scripts/validate_distribution.py` extracts an archive and enforces validation gates used by CI.

### State Tracking

- `scripts/update_release_state.py` writes `release-state/latest.json` plus `release-state/history/*.json` snapshots.

## Scripts Reference

- `scripts/build_portable.py`: Build and package portable Python archives.
- `scripts/check_stdlib.py`: Import-check stdlib modules in fast/full modes.
- `scripts/validate_distribution.py`: Validate packaged archive and optional stdlib set comparison.
- `scripts/resolve_latest_patch.py`: Resolve latest CPython patch tags per major.
- `scripts/plan_release.py`: Compute build/release plan from resolved versions and current state.
- `scripts/update_release_state.py`: Persist latest/history release-state snapshots.

## Project Structure

```text
python-builds/
|-- .github/workflows/          # Build, validation, and release automation
|-- config/majors.json          # Major version lines to track
|-- scripts/                    # Build/validation/planning tooling
|-- release-state/              # Committed release snapshots
|   |-- latest.json
|   `-- history/*.json
`-- README.md
```

## Notes

- Artifacts include `python/` plus `METADATA.json`.
- Linux and macOS archives are `.tar.gz`; Windows archives are `.zip`.
- Workflow releases use release-state snapshots as the source of truth for what changed.
