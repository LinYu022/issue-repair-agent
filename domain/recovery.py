"""Trusted recovery review/decision contracts. Historical grants cannot approve these requests."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from domain.plans import PlanRevision


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RecoveryRequest(Contract):
    recovery_id: UUID
    task_id: UUID
    rebuild_missing_workspace: bool = Field(default=False, strict=True)


class RecoveryReview(Contract):
    request: RecoveryRequest
    status: Literal["READY", "WAITING_PROCESSES", "SAFE_STOPPED", "CONFLICT", "CANCELLED"]
    reason: str
    stages: tuple[str, ...]
    checkpoint_id: UUID | None = None
    source_fingerprint: str | None = None
    runtime_fingerprint: str | None = None
    ledger_fingerprint: str | None = None
    authority_fingerprint: str | None = None
    trace_fingerprint: str | None = None
    ownership_fingerprint: str | None = None
    trace_entries: tuple[dict, ...] = ()
    expected_manifest: dict | None = None
    new_plan: PlanRevision | None = None
    resolutions: tuple[dict, ...] = ()
    original_authorization_state: str
    fresh_approval_required: Literal[True] = True


class RecoveryDecision(Contract):
    recovery_id: UUID
    review_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "deny"]
    expires_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=2000)
