# release-state

This directory is updated automatically by the release workflow.

- `latest.json` records the most recent released version map and release metadata.
- `history/*.json` stores one snapshot per release tag.

The workflow compares the newly resolved latest CPython patch releases against `latest.json` to decide which majors need to be rebuilt.