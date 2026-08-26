from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path

from .store import CheckpointRecord


@dataclass(frozen=True)
class ProbeResult:
    outcome: str
    reason: str
    evidence: str | None = None


class ResultReconciler:
    def probe(
        self,
        checkpoint: CheckpointRecord,
        *,
        terminal_observed: bool = False,
    ) -> ProbeResult:
        kind = checkpoint.probe_kind
        if kind == "backend-terminal":
            if terminal_observed:
                return ProbeResult("completed", "positive backend terminal event")
            return ProbeResult("uncertain", "backend terminal event not observed")

        if kind == "file-exists":
            if not checkpoint.probe_target:
                return ProbeResult("uncertain", "file-exists probe has no target")
            target = Path(checkpoint.probe_target)
            if target.is_file():
                return ProbeResult("completed", "declared output file exists", str(target))
            return ProbeResult("missing", "declared output file is missing", str(target))

        if kind == "file-sha256":
            if not checkpoint.probe_target or not checkpoint.expected_value:
                return ProbeResult("uncertain", "file-sha256 probe is incomplete")
            target = Path(checkpoint.probe_target)
            if not target.is_file():
                return ProbeResult("missing", "declared output file is missing", str(target))
            actual = self._sha256(target)
            if actual == checkpoint.expected_value.lower():
                return ProbeResult("completed", "output hash matches", actual)
            return ProbeResult("missing", "output hash does not match", actual)

        if kind is None:
            return ProbeResult("uncertain", "checkpoint has no result probe")
        return ProbeResult("uncertain", f"unsupported result probe: {kind}")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
