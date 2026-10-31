"""Task execution state, separate from tool results and hidden evaluation."""

from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class TaskLifecycle(StrEnum):
    CREATED = "CREATED"
    WAITING_INPUT = "WAITING_INPUT"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SelfTestStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class RunLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_tool_calls: int = Field(gt=0, strict=True)
    max_model_calls: int = Field(default=2, gt=0, strict=True)
    max_wall_seconds: int = Field(gt=0, strict=True)


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID = Field(default_factory=uuid4)
    source_task_id: str = Field(min_length=1)
    source_kind: Literal["public_benchmark", "self_built", "contract_fixture"]
    repo: str = Field(min_length=1)
    base_commit: str | None = Field(pattern=r"^[0-9a-f]{40}$")
    issue: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    available_at: AwareDatetime
    lifecycle: TaskLifecycle = TaskLifecycle.CREATED
    self_test_status: SelfTestStatus = SelfTestStatus.NOT_RUN
    limits: RunLimits
    tool_calls_reserved: int = Field(default=0, ge=0, strict=True)
    model_calls_reserved: int = Field(default=0, ge=0, strict=True)
    grant_id: UUID | None = None
    revision_id: UUID | None = None
    workspace_version: str | None = None
    execution_epoch: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def check_consistency(self) -> "Task":
        if self.source_kind == "contract_fixture":
            if self.base_commit is not None or not self.repo.startswith("fixture/"):
                raise ValueError("fixture requires base_commit=null and a fixture/ repository")
        elif self.base_commit is None or self.base_commit == "0" * 40 or self.repo.startswith("fixture/"):
            raise ValueError("real task requires a nonzero commit and a non-fixture repository")
        if self.tool_calls_reserved > self.limits.max_tool_calls:
            raise ValueError("reserved tool calls cannot exceed the configured limit")
        if self.model_calls_reserved > self.limits.max_model_calls:
            raise ValueError("reserved model calls cannot exceed the configured limit")
        return self
