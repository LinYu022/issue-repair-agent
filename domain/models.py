"""P2-01: the smallest observable tool-call contract."""

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0, strict=True)


class ToolObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: UUID
    call_id: UUID
    tool_name: Literal[
        "fake_tool",
        "workspace_list",
        "workspace_read",
        "workspace_search",
        "workspace_edit",
        "workspace_run_test",
        "workspace_diff",
        "workspace_symbols",
        "workspace_investigate",
        "artifact_browse",
        "episode_memory",
    ] = "fake_tool"
    evidence_kind: Literal["simulation", "real_execution"] = "simulation"
    status: ToolStatus
    exit_code: int | None = Field(strict=True)
    recorded_at: datetime
    artifact: ArtifactRef

    @model_validator(mode="after")
    def check_result(self) -> "ToolObservation":
        if (self.tool_name == "fake_tool") != (self.evidence_kind == "simulation"):
            raise ValueError("tool_evidence_kind_mismatch")
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must include a timezone")
        if self.status == ToolStatus.UNKNOWN and self.exit_code is not None:
            raise ValueError("UNKNOWN cannot claim a confirmed exit code")
        if self.status == ToolStatus.SUCCEEDED and self.exit_code != 0:
            raise ValueError("SUCCEEDED requires exit_code=0")
        if self.status == ToolStatus.FAILED and (self.exit_code is None or self.exit_code == 0):
            raise ValueError("FAILED requires a nonzero exit code")
        return self
