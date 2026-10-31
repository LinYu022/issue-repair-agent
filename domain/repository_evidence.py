"""Versioned source evidence and separately stored, explicitly unverified hypotheses."""

import hashlib
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from domain.models import ArtifactRef
from domain.workspaces import relative_parts


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceExcerpt(Contract):
    path: str
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)
    text: str = Field(max_length=8192)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def coherent(self):
        relative_parts(self.path)
        if self.end_line < self.start_line or self.end_line - self.start_line >= 200:
            raise ValueError("invalid_evidence_range")
        if len(self.text.encode()) > 8192 or hashlib.sha256(self.text.encode()).hexdigest() != self.text_sha256:
            raise ValueError("evidence_quote_integrity")
        normalized = self.text.replace("\r\n", "\n").replace("\r", "\n")
        count = 0 if not normalized else len(normalized.split("\n")) - int(normalized.endswith("\n"))
        if count != self.end_line - self.start_line + 1:
            raise ValueError("evidence_quote_line_count")
        return self


class RepositoryObservation(Contract):
    schema_version: Literal[1] = 1
    observation_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    workspace_id: UUID
    workspace_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    call_id: UUID
    operation: Literal["read", "search", "symbols", "investigate"]
    kind: Literal["source_excerpt", "static_definition", "static_reference", "reported_location"]
    source: Literal["workspace_text", "python_ast", "reported_input"]
    excerpt: SourceExcerpt
    symbol: str | None = None
    static_resolution: str | None = None
    recorded_at: AwareDatetime
    evidence_artifact: ArtifactRef
    source_executed: Literal[False] = False

    @model_validator(mode="after")
    def correct_source(self):
        expected = {
            "source_excerpt": "workspace_text",
            "static_definition": "python_ast",
            "static_reference": "python_ast",
            "reported_location": "reported_input",
        }
        if expected[self.kind] != self.source:
            raise ValueError("observation_kind_source_mismatch")
        return self


class LocationHypothesis(Contract):
    schema_version: Literal[1] = 1
    hypothesis_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    workspace_id: UUID
    workspace_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim: str = Field(min_length=1, max_length=2000)
    supporting_observation_ids: tuple[UUID, ...] = Field(min_length=1, max_length=20)
    status: Literal["unverified"] = "unverified"
    created_at: AwareDatetime
