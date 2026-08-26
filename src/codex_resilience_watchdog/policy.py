from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json

from .models import EffectClass, EvidenceClass, Observation


REAL_PROGRESS_KINDS = frozenset(
    {
        "stream_event",
        "tool_completed",
        "model_request",
        "backend_terminal",
        "output_changed",
        "counter_advanced",
        "checkpoint",
    }
)

PROGRESS_EVIDENCE = frozenset(
    {
        EvidenceClass.POSITIVE_PROGRESS,
        EvidenceClass.POSITIVE_TERMINAL,
        EvidenceClass.RESULT_PROBE,
    }
)


@dataclass(frozen=True)
class CircuitSnapshot:
    recovery_count: int
    restart_count: int
    same_fingerprint_count: int
    state_integrity: bool
    cli_compatible: bool
    recovery_attempted: bool
    recovery_had_progress: bool
    restart_requested: bool


def may_auto_replay(effect: EffectClass, repeatable: bool) -> bool:
    return effect is EffectClass.READ_ONLY and repeatable is True


def is_real_progress(observation: Observation) -> bool:
    return (
        observation.evidence_class in PROGRESS_EVIDENCE
        and observation.kind in REAL_PROGRESS_KINDS
    )


def fault_fingerprint(
    *,
    detector: str,
    session_id: str,
    step_id: str,
    effect: EffectClass,
    last_positive_kind: str,
) -> str:
    material = json.dumps(
        {
            "detector": detector,
            "effect": effect.value,
            "last_positive_kind": last_positive_kind,
            "session_id": session_id,
            "step_id": step_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def circuit_reason(snapshot: CircuitSnapshot) -> str | None:
    if not snapshot.state_integrity:
        return "state-integrity"
    if not snapshot.cli_compatible:
        return "cli-incompatible"
    if snapshot.recovery_count >= 2:
        return "recovery-limit"
    if snapshot.restart_requested and snapshot.restart_count >= 1:
        return "restart-limit"
    if snapshot.same_fingerprint_count >= 2:
        return "same-fault-limit"
    if snapshot.recovery_attempted and not snapshot.recovery_had_progress:
        return "no-progress-after-recovery"
    return None
