"""Stdlib-only read contracts for the reviewed Linux workspace component."""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

ReadOperation = Literal["list", "read"]


def relative_parts(path: str, *, allow_root: bool = False) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or len(path.encode("utf-8")) > 1024:
        raise ValueError("invalid_relative_path")
    if allow_root and path == ".":
        return ()
    parsed = PurePosixPath(path)
    if (
        parsed.is_absolute()
        or not parsed.parts
        or str(parsed) != path
        or any(part in {".", ".."} for part in parsed.parts)
        or any(char in path for char in "\\:")
        or any(ord(char) < 32 for char in path)
    ):
        raise ValueError("invalid_relative_path")
    if any(
        part.casefold() in {".git", ".local", ".env"} or part.casefold().startswith(".env.") for part in parsed.parts
    ):
        raise ValueError("protected_workspace_path")
    return parsed.parts


@dataclass(frozen=True)
class WorkspaceIdentity:
    task_id: UUID
    workspace_id: UUID
    repo: str
    base_commit: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, UUID) or not isinstance(self.workspace_id, UUID):
            raise ValueError("workspace_uuid_required")
        if not isinstance(self.repo, str) or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("invalid_workspace_repository")
        if self.repo.startswith("fixture/"):
            if self.base_commit is not None:
                raise ValueError("fixture_base_must_be_null")
        elif (
            not isinstance(self.base_commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}", self.base_commit)
            or self.base_commit == "0" * 40
        ):
            raise ValueError("full_workspace_base_required")


@dataclass(frozen=True)
class ReadGrant:
    grant_id: UUID
    identity: WorkspaceIdentity
    paths: tuple[str, ...]
    operations: frozenset[ReadOperation]
    expires_at: datetime
    max_read_bytes: int = 4096
    max_list_entries: int = 64
    revoked: bool = False
    issuer: Literal["development_fixture"] = "development_fixture"

    def __post_init__(self) -> None:
        if not isinstance(self.grant_id, UUID) or not isinstance(self.identity, WorkspaceIdentity):
            raise ValueError("invalid_grant_identity")
        if self.issuer != "development_fixture" or not self.identity.repo.startswith("fixture/"):
            raise ValueError("only_preapproved_development_fixture_grants_supported")
        if type(self.paths) is not tuple or not self.paths or len(set(self.paths)) != len(self.paths):
            raise ValueError("explicit_unique_grant_paths_required")
        for path in self.paths:
            relative_parts(path, allow_root=True)
        if type(self.operations) is not frozenset or not self.operations or not self.operations <= {"list", "read"}:
            raise ValueError("read_only_operations_required")
        if (
            not isinstance(self.expires_at, datetime)
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("aware_grant_expiry_required")
        for value, upper in ((self.max_read_bytes, 65536), (self.max_list_entries, 128)):
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError("bounded_positive_read_limits_required")
        if type(self.revoked) is not bool:
            raise ValueError("boolean_revocation_required")


@dataclass(frozen=True)
class ReadRequest:
    task_id: UUID
    workspace_id: UUID
    grant_id: UUID
    operation: ReadOperation
    path: str

    def __post_init__(self) -> None:
        if not all(isinstance(value, UUID) for value in (self.task_id, self.workspace_id, self.grant_id)):
            raise ValueError("request_uuid_required")
        if self.operation not in {"list", "read"}:
            raise ValueError("read_only_operation_required")
        relative_parts(self.path, allow_root=self.operation == "list")
