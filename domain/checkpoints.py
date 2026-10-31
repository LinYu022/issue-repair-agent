"""Joint checkpoint contracts. Stored approval references never grant new execution rights."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from domain.models import ArtifactRef
from domain.workspaces import relative_parts


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ManifestEntry(Contract):
    path: str
    kind: Literal["file", "directory"] = "file"
    content: ArtifactRef | None = None
    mode: int = Field(ge=0, le=0o777, strict=True)
    tracked: bool

    @model_validator(mode="after")
    def valid_entry(self):
        relative_parts(self.path)
        if (self.kind == "file") != (self.content is not None):
            raise ValueError("manifest_file_content_required")
        if self.content and self.content.size_bytes > 262144:
            raise ValueError("manifest_file_size_limit")
        return self


class WorkspaceManifest(Contract):
    schema_version: Literal[1] = 1
    task_id: UUID
    workspace_id: UUID
    repo: str
    base_commit: str | None
    root_mode: int = Field(ge=0, le=0o777, strict=True)
    entries: tuple[ManifestEntry, ...] = Field(max_length=1024)
    content_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    excluded: tuple[dict, ...] = ()

    @model_validator(mode="after")
    def complete_tree(self):
        paths = {e.path for e in self.entries}
        if len(paths) != len(self.entries) or len({p.casefold() for p in paths}) != len(paths):
            raise ValueError("manifest_duplicate_or_case_collision")
        directories = {e.path for e in self.entries if e.kind == "directory"}
        for path in paths:
            parts = relative_parts(path)
            if any("/".join(parts[:i]) not in directories for i in range(1, len(parts))):
                raise ValueError("manifest_parent_directory_missing")
        return self


class FileChange(Contract):
    operation: Literal["create", "modify", "delete", "metadata"]
    path: str
    before: ManifestEntry | None
    after: ManifestEntry | None

    @model_validator(mode="after")
    def consistent(self):
        relative_parts(self.path)
        if self.before is None and self.after is None:
            raise ValueError("file_change_needs_before_or_after")
        if any(e.path != self.path for e in (self.before, self.after) if e):
            raise ValueError("file_change_path_mismatch")
        return self


class JointGraphState(Contract):
    schema_version: Literal[1] = 1
    task_id: UUID
    task: dict
    control: dict
    loop: dict
    replanning: dict
    context: dict
    elapsed_wall_seconds: float = Field(ge=0)
    authority_on_load: Literal["audit_only_requires_live_reauthorization"] = "audit_only_requires_live_reauthorization"


class CheckpointDocument(Contract):
    schema_version: Literal[1] = 1
    checkpoint_id: UUID
    task_id: UUID
    workspace_id: UUID
    call_id: UUID
    lease_epoch: int = Field(gt=0, strict=True)
    execution_epoch: int = Field(gt=0, strict=True)
    workspace: ArtifactRef
    graph: ArtifactRef
    evidence: tuple[dict, ...]
    edit_objects: tuple[ArtifactRef, ...] = ()
    environment: dict
    ledger: dict
    control_stamp: str
    previous_checkpoint_id: UUID | None
    changes: tuple[FileChange, ...] = ()
    inferred_renames: tuple[dict, ...] = ()
    created_at: AwareDatetime


def manifest_changes(before: WorkspaceManifest, after: WorkspaceManifest):
    old, new = ({e.path: e for e in m.entries} for m in (before, after))
    changes = []
    for path in sorted(old.keys() | new.keys()):
        a, b = old.get(path), new.get(path)
        if a == b:
            continue
        operation: Literal["create", "delete", "metadata", "modify"] = (
            "create" if a is None else "delete" if b is None else "metadata" if a.content == b.content else "modify"
        )
        changes.append(FileChange(operation=operation, path=path, before=a, after=b))
    renames = []
    for deleted in changes:
        if deleted.operation != "delete" or deleted.before is None or deleted.before.kind != "file":
            continue
        matches = [
            c
            for c in changes
            if c.operation == "create" and c.after is not None and c.after.content == deleted.before.content
        ]
        sources = [
            c
            for c in changes
            if c.operation == "delete" and c.before is not None and c.before.content == deleted.before.content
        ]
        if len(matches) == len(sources) == 1:
            renames.append({"from": deleted.path, "to": matches[0].path, "basis": "unique_equal_content_inference"})
    return tuple(changes), tuple(renames)


def classify_changes(changes: tuple[FileChange, ...], actual: WorkspaceManifest):
    entries = {e.path: e for e in actual.entries}
    result = []
    for change in changes:
        current = entries.get(change.path)

        # Tracking is curator metadata, not a physical file property.
        def physical(item):
            return None if item is None else (item.kind, item.content, item.mode)

        state = (
            "new"
            if physical(current) == physical(change.after)
            else "old"
            if physical(current) == physical(change.before)
            else "missing"
            if current is None
            else "conflict"
        )
        result.append({"path": change.path, "state": state})
    return result
