from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "plan_release.py"


class PlanReleaseTests(unittest.TestCase):
    def _plan(self, tmp_path: Path, *, force: bool = False) -> dict:
        resolved = tmp_path / "resolved.json"
        state = tmp_path / "latest.json"
        resolved.write_text(
            json.dumps(
                {
                    "3.11": {
                        "version": "3.11.16",
                        "tag": "v3.11.16",
                        "tag_commit_sha": "a" * 40,
                    },
                    "3.12": {
                        "version": "3.12.14",
                        "tag": "v3.12.14",
                        "tag_commit_sha": "b" * 40,
                    },
                }
            ),
            encoding="utf-8",
        )
        state.write_text(
            json.dumps(
                {
                    "versions": {"3.11": "3.11.16", "3.12": "3.12.13"},
                    "details": {
                        "3.11": {
                            "version": "3.11.16",
                            "tag": "v3.11.16",
                            "tag_commit_sha": "a" * 40,
                        },
                        "3.12": {
                            "version": "3.12.13",
                            "tag": "v3.12.13",
                            "tag_commit_sha": "c" * 40,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        command = [
            sys.executable,
            str(SCRIPT),
            "--resolved-file",
            str(resolved),
            "--state-file",
            str(state),
        ]
        if force:
            command.append("--force")
        result = subprocess.run(command, check=True, capture_output=True, text=True)
        return json.loads(result.stdout)

    def test_version_bump_builds_only_changed_majors(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(Path(raw))

        self.assertTrue(plan["should_build"])
        self.assertEqual(plan["changed_majors"], ["3.12"])
        self.assertEqual(plan["changed_versions"], {"3.12": "3.12.14"})
        self.assertEqual(
            plan["release_tag"],
            "python-3.11.16-3.12.14",
        )

    def test_force_rebuilds_every_configured_major(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as raw:
            plan = self._plan(Path(raw), force=True)

        self.assertEqual(plan["changed_majors"], ["3.11", "3.12"])
        self.assertTrue(plan["should_build"])


if __name__ == "__main__":
    unittest.main()
