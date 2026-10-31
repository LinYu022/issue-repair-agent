"""Plans propose work. Only independently recorded operator decisions grant authority."""

import hashlib
import json
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from domain.tasks import RunLimits
from domain.workspace_tools import Operation, WorkspaceGrant
from domain.workspaces import WorkspaceIdentity, relative_parts

READ_ONLY: frozenset[Operation] = frozenset({"list", "read", "search", "symbols", "investigate", "diff"})


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def document_hash(value: BaseModel | dict) -> str:
    def canonical(item):
        if isinstance(item, BaseModel):
            return canonical(item.model_dump(mode="python"))
        if isinstance(item, dict):
            return {key: canonical(child) for key, child in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted((canonical(child) for child in item), key=lambda child: json.dumps(child, sort_keys=True))
        if isinstance(item, (tuple, list)):
            return [canonical(child) for child in item]
        if isinstance(item, (UUID, datetime)):
            return str(item)
        if isinstance(item, Enum):
            return item.value
        return item

    data = canonical(value)
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def path_subset(child: tuple[str, ...], parent: tuple[str, ...]) -> bool:
    return all(
        any(parts[: len(prefix)] == prefix for prefix in (relative_parts(p, allow_root=True) for p in parent))
        for parts in (relative_parts(p, allow_root=True) for p in child)
    )


class PlanScope(Contract):
    read_paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    write_paths: tuple[str, ...] = Field(default=(), max_length=64)
    operations: frozenset[Operation]
    test_profiles: tuple[str, ...] = Field(default=(), max_length=16)
    network: Literal["none"] = "none"
    limits: RunLimits
    max_output_bytes: int = Field(default=65536, gt=0, le=1048576, strict=True)
    max_test_seconds: int = Field(default=10, gt=0, le=120, strict=True)

    @model_validator(mode="after")
    def explicit_scope(self):
        if not self.operations:
            raise ValueError("explicit_plan_operations_required")
        for paths in (self.read_paths, self.write_paths):
            if len(set(paths)) != len(paths):
                raise ValueError("unique_plan_paths_required")
            for path in paths:
                relative_parts(path, allow_root=True)
        if not path_subset(self.write_paths, self.read_paths):
            raise ValueError("write_paths_must_be_readable")
        if "edit" in self.operations and not self.write_paths:
            raise ValueError("edit_requires_write_paths")
        if "run_test" in self.operations and (not self.test_profiles or "." not in self.read_paths):
            raise ValueError("tests_require_explicit_profiles_and_full_visible_package_read")
        return self

    def contained_by(self, other: "PlanScope") -> bool:
        return (
            path_subset(self.read_paths, other.read_paths)
            and path_subset(self.write_paths, other.write_paths)
            and self.operations <= other.operations
            and set(self.test_profiles) <= set(other.test_profiles)
            and all(
                getattr(self.limits, key) <= getattr(other.limits, key)
                for key in ("max_tool_calls", "max_model_calls", "max_wall_seconds")
            )
            and self.max_output_bytes <= other.max_output_bytes
            and self.max_test_seconds <= other.max_test_seconds
        )


class CandidateLocation(Contract):
    path: str
    line: int | None = Field(default=None, ge=1, strict=True)
    symbol: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def path_valid(self):
        relative_parts(self.path)
        return self


class PlanBody(Contract):
    repo: str
    base_commit: str | None
    understanding: str = Field(min_length=1, max_length=4000)
    candidates: tuple[CandidateLocation, ...] = Field(min_length=1, max_length=32)
    hypotheses: tuple[str, ...] = Field(min_length=1, max_length=16)
    change_strategy: tuple[str, ...] = Field(min_length=1, max_length=32)
    validation_strategy: tuple[str, ...] = Field(min_length=1, max_length=16)
    scope: PlanScope
    evidence_call_ids: tuple[UUID, ...] = Field(default=(), max_length=32)
    hypothesis_status: Literal["unverified"] = "unverified"

    @model_validator(mode="after")
    def valid_content(self):
        WorkspaceIdentity(uuid4(), uuid4(), self.repo, self.base_commit)
        for value in (*self.hypotheses, *self.change_strategy, *self.validation_strategy):
            if not value.strip() or len(value) > 2000:
                raise ValueError("bounded_plan_text_required")
        return self


class PlanProposal(Contract):
    request_id: UUID = Field(default_factory=uuid4)
    expected_revision_id: UUID | None
    expected_workspace_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=2000)
    body: PlanBody


class PlanRevision(Contract):
    schema_version: Literal[1] = 1
    revision_id: UUID = Field(default_factory=uuid4)
    plan_id: UUID
    task_id: UUID
    workspace_id: UUID
    parent_revision_id: UUID | None
    workspace_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str
    body: PlanBody
    created_at: AwareDatetime
    author_role: Literal["planner"] = "planner"
    linked_authorization_id: UUID | None = None
    environment_id: str
    image_id: str
    profile_hashes: tuple[tuple[str, str], ...]
    container_limits_hash: str


class ApprovalDecision(Contract):
    request_id: UUID = Field(default_factory=uuid4)
    revision_id: UUID
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_workspace_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["approve", "deny"]
    expires_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=2000)


class Authorization(Contract):
    authorization_id: UUID
    revision_id_at_approval: UUID
    revision_hash_at_approval: str
    grant: WorkspaceGrant
    scope: PlanScope
    environment_id: str
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    profile_hashes: tuple[tuple[str, str], ...]
    container_limits_hash: str
    approved_at: AwareDatetime
    provenance: Literal["development_fixture", "local_operator"]
    actor_role: Literal["approver"] = "approver"


class RevokeAuthorization(Contract):
    request_id: UUID = Field(default_factory=uuid4)
    authorization_id: UUID
    reason: str = Field(min_length=1, max_length=2000)


class HumanHint(Contract):
    request_id: UUID = Field(default_factory=uuid4)
    text: str = Field(min_length=1, max_length=4000)


class ControlRequest(Contract):
    request_id: UUID = Field(default_factory=uuid4)
    action: Literal["plan", "pause", "resume", "wait_input", "complete", "fail", "cancel"]
    expected_revision_id: UUID | None
    reason: str = Field(min_length=1, max_length=2000)
