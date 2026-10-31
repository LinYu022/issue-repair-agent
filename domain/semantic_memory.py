"""Semantic claims carry provenance and exact applicability; they never grant execution rights."""

import re
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from domain.workspaces import WorkspaceIdentity, relative_parts

Digest = str
Kind = Literal["preference", "convention", "command", "hypothesis"]
Origin = Literal["user_confirmation", "static_document", "visible_execution", "model_inference"]
State = Literal["CANDIDATE", "ACTIVE", "STALE", "CONFLICT", "REVOKED", "SUPERSEDED"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemoryScope(Contract):
    repo: str
    branches: tuple[str, ...] = Field(min_length=1, max_length=8)
    commits: tuple[str | None, ...] = Field(min_length=1, max_length=8)
    paths: tuple[str, ...] = Field(min_length=1, max_length=16)
    environment_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    workspace_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    watched_files: dict[str, str | None] = Field(default_factory=dict, max_length=512)

    @model_validator(mode="after")
    def explicit_scope(self):
        for commit in self.commits:
            WorkspaceIdentity(uuid4(), uuid4(), self.repo, commit)
        for branch in self.branches:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch) or ".." in branch:
                raise ValueError("explicit_memory_branch_required")
        for path in self.paths:
            relative_parts(path, allow_root=True)
        for path, digest in self.watched_files.items():
            relative_parts(path)
            if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("memory_watch_hash_required")
        if len(set(self.branches)) != len(self.branches) or len(set(self.commits)) != len(self.commits):
            raise ValueError("unique_memory_scope_required")
        return self


class RevisionRef(Contract):
    memory_id: UUID
    revision: int = Field(ge=1, strict=True)
    document_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SemanticSource(Contract):
    source_id: UUID
    user_id: UUID
    origin: Origin
    scope: MemoryScope
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    quote: str = Field(min_length=1, max_length=2400)
    evidence: dict
    dependencies: tuple[RevisionRef, ...] = Field(default=(), max_length=8)
    origin_boundary: Literal["operator_or_visible_task_only"] = "operator_or_visible_task_only"

    @model_validator(mode="after")
    def unique_dependencies(self):
        if len({p.memory_id for p in self.dependencies}) != len(self.dependencies):
            raise ValueError("unique_semantic_dependencies_required")
        return self


class SemanticDocument(Contract):
    schema_version: Literal[1] = 1
    memory_id: UUID
    revision: int = Field(ge=1, strict=True)
    user_id: UUID
    source_id: UUID
    kind: Kind
    key: str
    value: str
    scope: MemoryScope
    strength: Literal[
        "confirmed_preference", "document_assertion", "single_execution_observation", "unverified_inference"
    ]
    dependencies: tuple[RevisionRef, ...] = ()
    grants_authority: Literal[False] = False


class MemoryQuery(Contract):
    task_id: UUID
    repo: str
    branch: str
    base_commit: str | None
    paths: tuple[str, ...] = Field(min_length=1, max_length=16)
    environment_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_hashes: dict[str, str | None] = Field(max_length=512)
    as_of: AwareDatetime
    text: str = Field(default="", max_length=1000)
    limit: int = Field(default=6, ge=1, le=8, strict=True)

    @model_validator(mode="after")
    def valid_target(self):
        MemoryScope(
            repo=self.repo,
            branches=(self.branch,),
            commits=(self.base_commit,),
            paths=self.paths,
            watched_files=self.file_hashes,
        )
        return self


class MemoryReview(Contract):
    request_id: UUID
    memory_id: UUID
    expected_revision: int = Field(ge=1, strict=True)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["promote", "revoke"]
    reason: str = Field(min_length=1, max_length=1000)
    supersedes: tuple[RevisionRef, ...] = Field(default=(), max_length=8)


class MemoryAmend(Contract):
    request_id: UUID
    expected_revision: int = Field(ge=1, strict=True)
    expected_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_id: UUID
    reason: str = Field(min_length=1, max_length=1000)


class UserConfirmation(Contract):
    source_id: UUID
    scope: MemoryScope
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    text: str = Field(min_length=1, max_length=2000)
    dependencies: tuple[RevisionRef, ...] = ()


class EnvironmentChange(Contract):
    request_id: UUID
    repo: str
    branch: str
    changed_paths: tuple[str, ...] = Field(default=(), max_length=512)
    old_environment_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def valid_paths(self):
        for path in self.changed_paths:
            relative_parts(path)
        if not self.changed_paths and self.old_environment_hash is None:
            raise ValueError("explicit_memory_invalidation_target_required")
        return self
