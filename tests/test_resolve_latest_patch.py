from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from resolve_latest_patch import (  # noqa: E402
    details_for_version,
    latest_detail_for_major,
)


VALID_SHA = "0123456789abcdef" * 2 + "01234567"


class ResolveLatestPatchTests(unittest.TestCase):
    def test_latest_detail_requires_immutable_sha(self) -> None:
        refs = [
            {
                "name": "v3.13.15",
                "tag_commit_sha": VALID_SHA,
            },
        ]

        self.assertEqual(
            latest_detail_for_major(refs, "3.13"),
            {
                "version": "3.13.15",
                "tag": "v3.13.15",
                "tag_commit_sha": VALID_SHA,
            },
        )

    def test_missing_sha_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "immutable commit SHA"):
            latest_detail_for_major([{"name": "v3.13.15"}], "3.13")

    def test_explicit_version_requires_matching_tag_sha(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "immutable commit SHA"):
            details_for_version([{"name": "v3.13.15"}], "3.13.15")


if __name__ == "__main__":
    unittest.main()
