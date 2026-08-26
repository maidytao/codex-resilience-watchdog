from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TaskState(StrEnum):
    ARMED = "armed"
    RUNNING = "running"
    SUSPECT = "suspect"
    RECONCILING = "reconciling"
    RECOVERY_READY = "recovery-ready"
    RESTARTING = "restarting"
    RESUMING = "resuming"
    PENDING_CONFIRMATION = "pending-confirmation"
    COMPLETED = "completed"
    FAILED = "failed"
    CIRCUIT_OPEN = "circuit-open"


class EffectClass(StrEnum):
    READ_ONLY = "read-only"
    WRITE = "write"
    EXTERNAL_MESSAGE = "external-message"
    DELETE = "delete"
    PAID = "paid"
    UNKNOWN = "unknown"


class EvidenceClass(StrEnum):
    POSITIVE_PROGRESS = "positive-progress"
    POSITIVE_TERMINAL = "positive-terminal"
    ABSENCE_ONLY = "absence-only"
    RESULT_PROBE = "result-probe"
    SYNTHETIC = "synthetic"


@dataclass(frozen=True)
class Observation:
    kind: str
    evidence_class: EvidenceClass = EvidenceClass.ABSENCE_ONLY
    observed_at: datetime | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecoveryDecision:
    state: TaskState
    action: str
    reason: str
    task_id: str | None = None
    evidence: tuple[str, ...] = ()
