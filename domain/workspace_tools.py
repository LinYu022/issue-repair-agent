"""Explicit operator grants and bounded workspace tools; no host paths or shell."""

import hashlib
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from domain.workspaces import WorkspaceIdentity, relative_parts

Operation = Literal["list", "read", "search", "edit", "run_test", "diff", "symbols", "investigate"]
OPERATIONS: frozenset[Operation] = frozenset(
    {"list", "read", "search", "edit", "run_test", "diff", "symbols", "investigate"}
)


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VisibleTask(Contract):
    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=200)
    source_kind: Literal["contract_fixture", "public_benchmark", "self_built"]
    repo: str
    base_commit: str | None
    issue: str = Field(min_length=1, max_length=32768)
    environment_id: str = Field(min_length=1, max_length=200)
    available_at: AwareDatetime

    @model_validator(mode="after")
    def valid_identity(self):
        WorkspaceIdentity(uuid4(), uuid4(), self.repo, self.base_commit)
        if (self.source_kind == "contract_fixture") != self.repo.startswith("fixture/"):
            raise ValueError("fixture_source_identity_mismatch")
        return self


class VisibleFile(Contract):
    path: str
    text: str = Field(max_length=262144)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def check_bytes(self):
        relative_parts(self.path)
        data = self.text.encode("utf-8")
        if len(data) > 262144 or hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError("visible_file_integrity_or_size")
        return self


class TaskPackage(Contract):
    schema_version: Literal[1] = 1
    task: VisibleTask
    files: tuple[VisibleFile, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def valid_inventory(self):
        names = {entry.path.casefold() for entry in self.files}
        if len(names) != len(self.files) or sum(len(entry.text.encode()) for entry in self.files) > 4 * 1024 * 1024:
            raise ValueError("duplicate_or_oversize_inventory")
        for entry in self.files:
            parts = relative_parts(entry.path)
            if any("/".join(parts[:index]).casefold() in names for index in range(1, len(parts))):
                raise ValueError("file_directory_collision")
        return self


class TestProfile(Contract):
    # Trusted operator configuration, never populated from an Agent tool request.
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    argv: tuple[str, ...] = Field(min_length=2, max_length=32)
    cwd: str = "."

    @model_validator(mode="after")
    def valid_command(self):
        if self.argv[0] != "/usr/local/bin/python" or any("\0" in arg or len(arg) > 1024 for arg in self.argv):
            raise ValueError("python_test_profile_required")
        relative_parts(self.cwd, allow_root=True)
        return self


class WorkspaceGrant(Contract):
    grant_id: UUID = Field(default_factory=uuid4)
    identity: WorkspaceIdentity
    issuer: Literal["development_fixture", "operator"]
    paths: tuple[str, ...] = Field(min_length=1, max_length=64)
    write_paths: tuple[str, ...] | None = Field(default=None, max_length=64)
    operations: frozenset[Operation]
    test_profiles: tuple[str, ...] = ()
    expires_at: AwareDatetime
    revoked: bool = Field(default=False, strict=True)
    max_output_bytes: int = Field(default=65536, gt=0, le=1048576, strict=True)
    max_seconds: int = Field(default=10, gt=0, le=120, strict=True)

    @model_validator(mode="after")
    def explicit_scope(self):
        if self.issuer == "development_fixture" and not self.identity.repo.startswith("fixture/"):
            raise ValueError("fixture_grant_repository_mismatch")
        if not self.operations or len(set(self.paths)) != len(self.paths):
            raise ValueError("explicit_unique_scope_required")
        for path in self.paths:
            relative_parts(path, allow_root=True)
        if self.write_paths is not None:
            if len(set(self.write_paths)) != len(self.write_paths):
                raise ValueError("unique_write_scope_required")
            for path in self.write_paths:
                parts = relative_parts(path, allow_root=True)
                if not any(
                    parts[: len(prefix)] == prefix
                    for prefix in (relative_parts(p, allow_root=True) for p in self.paths)
                ):
                    raise ValueError("write_scope_must_be_readable")
        return self

    def check(self, identity, request, now: datetime):
        if self.identity != identity or (request.task_id, request.workspace_id) != (
            identity.task_id,
            identity.workspace_id,
        ):
            raise ValueError("workspace_identity_mismatch")
        if self.revoked or now >= self.expires_at:
            raise ValueError("grant_revoked_or_expired")
        if request.arguments.operation not in self.operations:
            raise ValueError("operation_not_granted")
        path = getattr(request.arguments, "path", ".")
        parts = relative_parts(path, allow_root=True)
        scope = (
            self.write_paths if request.arguments.operation == "edit" and self.write_paths is not None else self.paths
        )
        if not any(parts[: len(prefix)] == prefix for prefix in (relative_parts(p, allow_root=True) for p in scope)):
            raise ValueError("path_not_granted")
        destination = getattr(request.arguments, "destination", None)
        if destination is not None:
            target = relative_parts(destination)
            if not any(
                target[: len(prefix)] == prefix for prefix in (relative_parts(p, allow_root=True) for p in scope)
            ):
                raise ValueError("rename_destination_not_granted")
        if isinstance(request.arguments, RunTestArgs) and request.arguments.profile_id not in self.test_profiles:
            raise ValueError("test_execution_not_granted")


class ListArgs(Contract):
    operation: Literal["list"] = "list"
    path: str = "."


class ReadArgs(Contract):
    operation: Literal["read"] = "read"
    path: str
    start_line: int | None = Field(default=None, ge=1, strict=True)
    end_line: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def valid_range(self):
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("both_read_line_bounds_required")
        if (
            self.start_line is not None
            and self.end_line is not None
            and (self.end_line < self.start_line or self.end_line - self.start_line >= 200)
        ):
            raise ValueError("bounded_ordered_read_range_required")
        return self


class SearchArgs(Contract):
    operation: Literal["search"] = "search"
    path: str = "."
    query: str = Field(min_length=1, max_length=128)


class EditArgs(Contract):
    operation: Literal["edit"] = "edit"
    path: str
    expected_sha256: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    text: str | None = Field(max_length=262144)
    destination: str | None = None

    @model_validator(mode="after")
    def rename_contract(self):
        if self.destination is not None:
            relative_parts(self.destination)
            if self.destination == self.path or self.text is not None or self.expected_sha256 is None:
                raise ValueError("rename_requires_distinct_absent_target_and_source_hash")
        return self


class RunTestArgs(Contract):
    operation: Literal["run_test"] = "run_test"
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class DiffArgs(Contract):
    operation: Literal["diff"] = "diff"
    path: str = "."


class SymbolsArgs(Contract):
    operation: Literal["symbols"] = "symbols"
    path: str = "."
    query: str = Field(min_length=1, max_length=200)
    mode: Literal["definitions", "references"] = "definitions"
    limit: int = Field(default=20, ge=1, le=40, strict=True)
    context_lines: int = Field(default=1, ge=0, le=3, strict=True)

    @model_validator(mode="after")
    def exact_symbol(self):
        if not all(part.isidentifier() for part in self.query.split(".")):
            raise ValueError("exact_dotted_symbol_required")
        return self


class InvestigateArgs(Contract):
    operation: Literal["investigate"] = "investigate"
    path: str = "."
    issue: str = Field(default="", max_length=32768)
    traceback: str = Field(default="", max_length=16384)
    file_paths: tuple[str, ...] = Field(default=(), max_length=16)
    test_entries: tuple[str, ...] = Field(default=(), max_length=16)
    symbols: tuple[str, ...] = Field(default=(), max_length=16)
    limit: int = Field(default=12, ge=1, le=20, strict=True)
    context_lines: int = Field(default=1, ge=0, le=3, strict=True)

    @model_validator(mode="after")
    def visible_inputs(self):
        for path in self.file_paths:
            relative_parts(path)
        for entry in self.test_entries:
            components = entry.split("::")
            relative_parts(components[0])
            if len(components) > 4 or any(not part.isidentifier() for part in components[1:]):
                raise ValueError("exact_test_entry_required")
        for symbol in self.symbols:
            SymbolsArgs(query=symbol)
        return self


Arguments = Annotated[
    ListArgs | ReadArgs | SearchArgs | EditArgs | RunTestArgs | DiffArgs | SymbolsArgs | InvestigateArgs,
    Field(discriminator="operation"),
]


class WorkspaceToolRequest(Contract):
    task_id: UUID
    workspace_id: UUID
    grant_id: UUID
    execution_epoch: int = Field(ge=1, strict=True)
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision_id: UUID | None = None
    call_id: UUID = Field(default_factory=uuid4)
    arguments: Arguments

    @property
    def tool_name(self):
        return "workspace_" + self.arguments.operation

    @model_validator(mode="after")
    def valid_path(self):
        path = getattr(self.arguments, "path", ".")
        relative_parts(path, allow_root=not isinstance(self.arguments, (ReadArgs, EditArgs)))
        return self
