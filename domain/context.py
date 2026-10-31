"""Bounded context and task-local artifact contracts; summaries grant no authority."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from domain.models import ArtifactRef


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ContextSettings(Contract):
    mode: Literal["full", "window", "structured"] = "full"
    recent_messages: int = Field(default=4, ge=2, le=12, strict=True)
    recent_observations: int = Field(default=3, ge=1, le=8, strict=True)
    excerpt_chars: int = Field(default=800, ge=128, le=2000, strict=True)
    max_context_bytes: int = Field(default=196608, ge=8192, le=524288, strict=True)
    memory_enabled: bool = Field(default=False, strict=True)


class ArtifactSource(Contract):
    source_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    workspace_id: UUID
    kind: Literal[
        "feedback",
        "model_proposal",
        "source_code",
        "tool_observation",
        "process_stdout",
        "process_stderr",
        "controller_event",
        "hypothesis_revision",
    ]
    workspace_version: str
    producer_call_id: UUID | None = None
    path: str | None = None
    source_file_sha256: str | None = None
    artifact: ArtifactRef
    provenance: Literal[
        "tool_observation", "task_process_report", "model_unverified", "controller_record", "hypothesis_not_proven"
    ]
    recorded_at: AwareDatetime


class TranscriptEntry(Contract):
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    source_id: UUID
    workspace_version: str


class BrowseArtifact(Contract):
    operation: Literal["browse_artifact"] = "browse_artifact"
    source_id: UUID
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=2048, ge=1, le=4096, strict=True)


class ArtifactBrowseRequest(Contract):
    task_id: UUID
    workspace_id: UUID
    grant_id: UUID
    revision_id: UUID | None
    execution_epoch: int = Field(ge=1, strict=True)
    expected_version: str
    call_id: UUID = Field(default_factory=uuid4)
    arguments: BrowseArtifact

    @property
    def tool_name(self):
        return "artifact_browse"


class ArtifactPage(Contract):
    source: ArtifactSource
    version_state: Literal["current_version", "historical_version"]
    offset_unit: Literal["unicode_codepoints"] = "unicode_codepoints"
    offset: int
    next_offset: int | None
    total_characters: int
    text: str
    excerpt_sha256: str


class ObservationSummary(Contract):
    source_id: UUID
    call_id: UUID
    operation: str
    input_version: str
    output_version: str
    version_state: Literal["current_version", "historical_version"]
    status: str
    exit_code: int | None
    reason: str | None
    self_test_status_at_observation: str | None
    profile_id: str | None = None
    structured_test_metadata: dict | None = None
    excerpt: str
    excerpt_truncated: bool
    related_sources: tuple[UUID, ...] = ()


class StructuredState(Contract):
    schema_version: Literal[1] = 1
    task_id: UUID
    current_workspace_version: str
    current_plan: dict | None
    authorization: dict
    task_state: dict
    active_hypotheses: tuple[dict, ...]
    refuted_or_superseded_hypotheses: tuple[dict, ...]
    historical_hypotheses: tuple[dict, ...]
    failures_and_counterevidence: tuple[ObservationSummary, ...]
    recent_observations: tuple[ObservationSummary, ...]
    key_code: tuple[dict, ...]
    strategy_changes: tuple[dict, ...]
    classifications: tuple[dict, ...]
    operator_notes: tuple[dict, ...]
    expanded_pages: tuple[dict, ...]
    memory: dict
    source_of_truth: Literal["versioned_records_not_generated_root_cause"] = (
        "versioned_records_not_generated_root_cause"
    )


class ContextBuildRecord(Contract):
    build_id: UUID = Field(default_factory=uuid4)
    mode: Literal["full", "window", "structured"]
    task_id: UUID
    state_stamp: str
    transcript_entries: int
    prompt_artifact: ArtifactRef
    summary_artifact: ArtifactRef | None
    prefix_sha256: str
    prefix_utf8_bytes: int
    prompt_utf8_bytes: int
    prompt_characters: int
    preparation_wall_seconds: float
    extra_summary_model_calls: Literal[0] = 0
    extra_summary_model_tokens: Literal[0] = 0
    token_measurement: Literal["bytes_are_not_provider_tokens"] = "bytes_are_not_provider_tokens"

    @model_validator(mode="after")
    def prompt_size_agrees(self):
        if self.prompt_artifact.size_bytes != self.prompt_utf8_bytes:
            raise ValueError("context_prompt_byte_count_mismatch")
        return self
