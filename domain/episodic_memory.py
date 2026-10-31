"""Bounded historical episodes and read-only Agent navigation; history never grants authority."""

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.models import ArtifactRef
from domain.semantic_memory import MemoryQuery, MemoryScope, RevisionRef
from domain.workspaces import relative_parts


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CodeLocation(Contract):
    path: str
    line: int | None = Field(default=None, ge=1, strict=True)
    workspace_version: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_path(self):
        relative_parts(self.path)
        return self


class EpisodeEvent(Contract):
    anchor_id: UUID
    sequence: int = Field(ge=1, strict=True)
    kind: Literal["symptom", "hypothesis", "tool", "strategy", "final"]
    label: Literal[
        "source_issue_unverified",
        "model_hypothesis_unverified",
        "visible_tool_observation",
        "controller_strategy",
        "visible_task_outcome",
    ]
    excerpt: str = Field(max_length=900)
    text: ArtifactRef
    text_truncated: bool = Field(strict=True)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    call_id: UUID | None = None
    status: Literal["SUCCEEDED", "FAILED", "UNKNOWN"] | None = None
    operation: str | None = None
    location: CodeLocation | None = None


class EpisodeRef(Contract):
    episode_id: UUID
    document_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class EpisodeDocument(Contract):
    schema_version: Literal[1] = 1
    episode_id: UUID
    user_id: UUID
    source_task_id: UUID
    source_instance_id: str
    scope: MemoryScope
    source_initial_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_final_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    symptom: str = Field(min_length=1, max_length=1600)
    hypotheses: tuple[str, ...] = Field(max_length=32)
    outcome: Literal["self_test_passed", "failed", "paused", "cancelled", "unconfirmed"]
    outcome_reason: str = Field(max_length=2000)
    counterevidence: tuple[UUID, ...] = Field(max_length=256)
    events: tuple[EpisodeEvent, ...] = Field(min_length=2, max_length=256)
    semantic_dependencies: tuple[RevisionRef, ...] = Field(default=(), max_length=16)
    parent_episodes: tuple[EpisodeRef, ...] = Field(default=(), max_length=32)
    independent_evaluation: Literal["not_imported"] = "not_imported"
    grants_authority: Literal[False] = False

    @model_validator(mode="after")
    def consistent(self):
        if [e.sequence for e in self.events] != list(range(1, len(self.events) + 1)) or len(
            {e.anchor_id for e in self.events}
        ) != len(self.events):
            raise ValueError("episode_sequence_or_anchor_conflict")
        failures = {e.anchor_id for e in self.events if e.status in {"FAILED", "UNKNOWN"}}
        if set(self.counterevidence) != failures:
            raise ValueError("episode_failure_labels_must_be_complete")
        if self.scope.workspace_version is not None:
            raise ValueError("episode_history_requires_explicit_version_labels_not_current_code")
        if len({p.memory_id for p in self.semantic_dependencies}) != len(self.semantic_dependencies):
            raise ValueError("unique_episode_dependencies_required")
        if self.episode_id in {r.episode_id for r in self.parent_episodes} or len(
            {r.episode_id for r in self.parent_episodes}
        ) != len(self.parent_episodes):
            raise ValueError("episode_parent_cycle_or_duplicate")
        return self


class EpisodeQuery(MemoryQuery):
    mode: Literal["search", "recent", "session"] = "search"
    source_task_id: UUID | None = None

    @model_validator(mode="after")
    def session_target(self):
        if self.mode == "session" and self.source_task_id is None:
            raise ValueError("episode_session_target_required")
        if self.mode != "session" and self.source_task_id is not None:
            raise ValueError("episode_source_task_only_for_session_query")
        return self


class SearchEpisodes(Contract):
    operation: Literal["search_episodes"] = "search_episodes"
    text: str = Field(default="", max_length=1000)
    mode: Literal["search", "recent", "session"] = "search"
    source_task_id: UUID | None = None
    limit: int = Field(default=5, ge=1, le=8, strict=True)

    @model_validator(mode="after")
    def valid_target(self):
        if (self.mode == "session") != (self.source_task_id is not None):
            raise ValueError("episode_session_target_required")
        return self


class BrowseEpisode(Contract):
    operation: Literal["browse_episode"] = "browse_episode"
    episode_id: UUID
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    anchor_id: UUID
    before: int = Field(default=1, ge=0, le=2, strict=True)
    after: int = Field(default=1, ge=0, le=2, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=1800, ge=1, le=4096, strict=True)


class AssessEpisode(Contract):
    operation: Literal["assess_episode"] = "assess_episode"
    episode_id: UUID
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: Literal["adopt", "reject"]
    reason: str = Field(min_length=1, max_length=1500)


EpisodeAction = Annotated[SearchEpisodes | BrowseEpisode | AssessEpisode, Field(discriminator="operation")]


class EpisodeToolRequest(Contract):
    task_id: UUID
    workspace_id: UUID
    grant_id: UUID
    revision_id: UUID | None
    execution_epoch: int = Field(ge=1, strict=True)
    expected_version: str
    call_id: UUID = Field(default_factory=uuid4)
    arguments: EpisodeAction

    @property
    def tool_name(self):
        return "episode_memory"


class EpisodeWithdrawal(Contract):
    request_id: UUID
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=1500)


class AnalogyVerdict(Contract):
    request_id: UUID
    consumer_task_id: UUID
    episode_id: UUID
    linked_call_id: UUID
    verdict: Literal["correct", "incorrect", "undetermined"]
    reason: str = Field(min_length=1, max_length=2000)
    call_record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verifier: Literal["visible_development_rule"] = "visible_development_rule"
    is_model_claim: Literal[False] = False
