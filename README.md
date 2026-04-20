# python-builds

Build portable CPython archives from source with the full standard library included.

Supported targets:

- Windows x86_64
- Linux x86_64
- macOS x86_64
- macOS arm64

Supported major versions are configured in `config/majors.json`.

## Usage

### Local build

Build the latest patch release for a major version:

```bash
python scripts/build_portable.py 3.13
```

Optional target overrides:

```bash
python scripts/build_portable.py 3.13 --target-os windows --target-arch x86_64 --output-dir dist
```

Artifacts are written to `dist/`.

### Validate a built archive

Quick import check:

```bash
python scripts/check_stdlib.py --mode fast
```

Full stdlib import sweep:

```bash
python scripts/check_stdlib.py --mode full --timeout-seconds 5
```

Validate an archive and compare it against another Python interpreter:

```bash
python scripts/validate_distribution.py dist/python-3.13.13-windows-x86_64.zip --baseline-python python
```

### GitHub Actions

Main workflow:

- `.github/workflows/build-python.yml`

It resolves the latest patch versions every week, compares them against the committed `release-state/latest.json` snapshot, builds only the majors that changed, validates the resulting artifacts, records a new in-repo release-state snapshot, and publishes a GitHub release and tag automatically.

Each packaged artifact also records the upstream CPython Git tag and tag commit SHA in `METADATA.json` alongside the python.org source URL and source checksum.
