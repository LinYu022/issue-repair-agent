"""A shared index for completed model/tool dispatch attempts."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from domain.models import ArtifactRef


class CallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: UUID
    call_id: UUID
    call_kind: Literal["model", "tool"]
    operation: str
    status: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]
    evidence_kind: Literal["simulation", "real_execution"] = "simulation"
    recorded_at: AwareDatetime
    request_artifact: ArtifactRef
    result_artifact: ArtifactRef
    grant_id: UUID | None = None
    revision_id: UUID | None = None
    workspace_version: str | None = None
    execution_epoch: int | None = Field(default=None, ge=1, strict=True)
