from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest


class TemporaryHomeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary_directory = TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.profile = self.root / "profile"
        self.codex_home = self.profile / ".codex"
        self.codex_home.mkdir(parents=True)

    def tearDown(self) -> None:
        self._temporary_directory.cleanup()
