"""Append-only runtime event contracts and projected control state."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


EventType = Literal[
    "RunCreated", "PlanProposed", "PlanApproved", "ActionProposed", "ToolCallPrepared",
    "ToolCallStarted", "ToolObservationReceived", "FailureClassified", "ReplanTriggered",
    "ContextBuilt", "ContextCompacted", "CheckpointCommitted", "RunPaused", "RunResumed",
    "RunStopped", "RunCompleted",
]
EventStatus = Literal["recorded", "succeeded", "failed", "unknown"]


class RunManifest(Contract):
    run_id: UUID = Field(default_factory=uuid4)
    task_hash: str = Field(min_length=1)
    package_hash: str = Field(min_length=1)
    registration_hash: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    model_identity: str = Field(min_length=1)
    adapter_identity: str = Field(min_length=1)
    runner_image: str = Field(min_length=1)
    environment: dict[str, Any] = {}
    resource_limits: dict[str, Any] = {}
    initial_workspace_version: str = Field(min_length=1)
    capability_snapshot_hash: str = Field(min_length=1)
    context_config: dict[str, Any] = {}
    replan_config: dict[str, Any] = {}
    replay_config: dict[str, Any] = {}
    evaluation_scope: dict[str, Any] = {}

    @classmethod
    def from_inputs(
        cls,
        *,
        task: Any,
        package: Any,
        registration: Any,
        config: Any,
        model_identity: str,
        adapter_identity: str,
        runner_image: str,
        initial_workspace_version: str,
        capability_snapshot_hash: str,
        environment: dict[str, Any] | None = None,
        resource_limits: dict[str, Any] | None = None,
        context_config: dict[str, Any] | None = None,
        replan_config: dict[str, Any] | None = None,
        replay_config: dict[str, Any] | None = None,
        evaluation_scope: dict[str, Any] | None = None,
    ) -> "RunManifest":
        def digest(value: Any) -> str:
            if hasattr(value, "model_dump"):
                value = value.model_dump(mode="json")
            return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return cls(task_hash=digest(task), package_hash=digest(package), registration_hash=digest(registration), config_hash=digest(config), model_identity=model_identity, adapter_identity=adapter_identity, runner_image=runner_image, initial_workspace_version=initial_workspace_version, capability_snapshot_hash=capability_snapshot_hash, environment=environment or {}, resource_limits=resource_limits or {}, context_config=context_config or {}, replan_config=replan_config or {}, replay_config=replay_config or {}, evaluation_scope=evaluation_scope or {})


class RunEvent(Contract):
    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    parent_event_id: UUID | None = None
    sequence_no: int = Field(ge=1, strict=True)
    event_type: EventType
    task_version: str
    authorization_revision: int = Field(default=1, ge=1, strict=True)
    capability_snapshot_id: UUID | None = None
    context_revision: int = Field(default=0, ge=0, strict=True)
    workspace_version: str
    payload_artifact_id: UUID | None = None
    payload: dict[str, Any] = {}
    payload_hash: str | None = None
    occurred_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    actor: Literal["controller", "model", "tool", "worker", "operator", "replay"]
    status: EventStatus = "recorded"

    @model_validator(mode="after")
    def hash_payload(self):
        computed = hashlib.sha256(json.dumps(self.payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if self.payload_hash is not None and self.payload_hash != computed:
            raise ValueError("event_payload_hash_mismatch")
        object.__setattr__(self, "payload_hash", computed)
        return self


class ProjectedRunState(Contract):
    run_id: UUID
    last_sequence: int = 0
    last_event_id: UUID | None = None
    status: str = "created"
    plan: dict[str, Any] | None = None
    authorization_revision: int = 1
    workspace_version: str | None = None
    context_revision: int = 0
    observations: tuple[dict[str, Any], ...] = ()
    replans: tuple[dict[str, Any], ...] = ()
    stop_reason: str | None = None

