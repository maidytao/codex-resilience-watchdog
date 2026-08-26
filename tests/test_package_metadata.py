from __future__ import annotations

from pathlib import Path
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PackageMetadataTest(unittest.TestCase):
    def test_public_package_metadata_points_to_canonical_repository(self) -> None:
        metadata = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]

        self.assertEqual(metadata.get("readme"), "README.md")
        self.assertEqual(metadata.get("license"), "MIT")
        self.assertEqual(metadata.get("authors"), [{"name": "maidytao"}])
        self.assertEqual(
            metadata["urls"]["Repository"],
            "https://github.com/maidytao/codex-resilience-watchdog",
        )
        self.assertEqual(
            metadata["urls"]["Issues"],
            "https://github.com/maidytao/codex-resilience-watchdog/issues",
        )


if __name__ == "__main__":
    unittest.main()
