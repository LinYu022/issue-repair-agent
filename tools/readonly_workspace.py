"""Descriptor-relative Linux reads; not an OS sandbox or arbitrary-code runner."""

import errno
import hashlib
import os
import stat
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from domain.workspaces import ReadGrant, ReadRequest, WorkspaceIdentity, relative_parts


class WorkspaceRefused(ValueError):
    """A bounded public refusal reason, without host paths or file content."""


class ReadLimits(Protocol):
    @property
    def max_read_bytes(self) -> int: ...


def require_linux_capabilities() -> None:
    if (
        not sys.platform.startswith("linux")
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or os.open not in os.supports_dir_fd
        or os.scandir not in os.supports_fd
    ):
        raise WorkspaceRefused("descriptor_backend_unavailable")


def kernel_flags(*names: str) -> int:
    result = os.O_RDONLY
    for name in names:
        flag = getattr(os, name, None)
        if not isinstance(flag, int):
            raise WorkspaceRefused("descriptor_backend_unavailable")
        result |= flag
    return result


def directory_flags() -> int:
    return kernel_flags("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")


def open_root(root: Path) -> int:
    require_linux_capabilities()
    if not root.is_absolute():
        raise WorkspaceRefused("absolute_operator_root_required")
    descriptor = os.open("/", directory_flags())
    try:
        for part in root.parts[1:]:
            next_descriptor = os.open(part, directory_flags(), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def stamp(status: os.stat_result) -> tuple[int, ...]:
    return status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns, status.st_nlink


def authorize_read(
    identity: WorkspaceIdentity, grants: Mapping[UUID, ReadGrant], request: ReadRequest, *, now: datetime
) -> ReadGrant:
    grant = grants.get(request.grant_id)
    if grant is None or grant.identity != identity or grant.grant_id != request.grant_id:
        raise WorkspaceRefused("grant_identity_mismatch")
    if (request.task_id, request.workspace_id) != (identity.task_id, identity.workspace_id):
        raise WorkspaceRefused("request_identity_mismatch")
    if now.tzinfo is None or now.utcoffset() is None:
        raise WorkspaceRefused("aware_clock_required")
    if grant.revoked or now >= grant.expires_at:
        raise WorkspaceRefused("grant_revoked_or_expired")
    parts = relative_parts(request.path, allow_root=request.operation == "list")
    if request.operation not in grant.operations or not any(
        parts[: len(prefix)] == prefix for prefix in (relative_parts(path) for path in grant.paths)
    ):
        raise WorkspaceRefused("path_or_operation_not_granted")
    return grant


class ReadOnlyWorkspace:
    def __init__(
        self,
        root: Path,
        identity: WorkspaceIdentity,
        grants: Mapping[UUID, ReadGrant],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.root, self.identity, self.grants, self.clock = root, identity, grants, clock
        try:
            descriptor = open_root(root)
        except OSError:
            raise WorkspaceRefused("workspace_root_unavailable") from None
        try:
            status = os.fstat(descriptor)
            self.root_identity = status.st_dev, status.st_ino
        finally:
            os.close(descriptor)

    def authorize(self, request: ReadRequest) -> ReadGrant:
        # Re-read the trusted registry on every call; no copy of the grant is cached.
        return authorize_read(self.identity, self.grants, request, now=self.clock())

    def execute(self, request: ReadRequest) -> dict[str, Any]:
        grant = self.authorize(request)
        parts = relative_parts(request.path)
        try:
            descriptor = open_root(self.root)
            try:
                status = os.fstat(descriptor)
                if (status.st_dev, status.st_ino) != self.root_identity:
                    raise WorkspaceRefused("workspace_root_replaced")
                for part in parts[:-1]:
                    child = os.open(part, directory_flags(), dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                if request.operation == "read":
                    result = self.read_file(descriptor, parts[-1], grant)
                else:
                    child = os.open(parts[-1], directory_flags(), dir_fd=descriptor)
                    try:
                        result = self.list_directory(child, grant, parts)
                    finally:
                        os.close(child)
            finally:
                os.close(descriptor)
        except OSError as exc:
            reason = "workspace_entry_missing" if exc.errno == errno.ENOENT else "link_or_entry_unavailable"
            raise WorkspaceRefused(reason) from None
        return {
            "task_id": str(request.task_id),
            "workspace_id": str(request.workspace_id),
            "grant_id": str(request.grant_id),
            "operation": request.operation,
            "path": request.path,
            **result,
        }

    @staticmethod
    def read_file(parent: int, name: str, grant: ReadLimits) -> dict[str, Any]:
        descriptor = os.open(name, kernel_flags("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"), dir_fd=parent)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise WorkspaceRefused("regular_single_link_file_required")
            if before.st_size > grant.max_read_bytes:
                raise WorkspaceRefused("read_output_limit")
            chunks, remaining = [], grant.max_read_bytes + 1
            while remaining:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if stamp(before) != stamp(after) or len(data) != after.st_size:
                raise WorkspaceRefused("file_changed_during_read")
            if len(data) > grant.max_read_bytes:
                raise WorkspaceRefused("read_output_limit")
            try:
                text = data.decode("utf-8")
            except UnicodeError:
                raise WorkspaceRefused("utf8_text_required") from None
            return {"text": text, "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        finally:
            os.close(descriptor)

    @staticmethod
    def list_directory(descriptor: int, grant: ReadGrant, parts: tuple[str, ...]) -> dict[str, Any]:
        before = os.fstat(descriptor)
        entries = []
        with os.scandir(descriptor) as iterator:
            for scanned, entry in enumerate(iterator, 1):
                if scanned > grant.max_list_entries:
                    raise WorkspaceRefused("list_output_limit")
                try:
                    relative_parts("/".join((*parts, entry.name)))
                except ValueError:
                    continue  # Protected metadata/credential names are not exported.
                status = entry.stat(follow_symlinks=False)
                kind = (
                    "blocked_link"
                    if stat.S_ISLNK(status.st_mode)
                    else "directory"
                    if stat.S_ISDIR(status.st_mode)
                    else "file"
                )
                if kind == "file" and (not stat.S_ISREG(status.st_mode) or status.st_nlink != 1):
                    kind = "blocked_special_or_hardlink"
                entries.append({"name": entry.name, "kind": kind})
        if stamp(before) != stamp(os.fstat(descriptor)):
            raise WorkspaceRefused("directory_changed_during_list")
        return {"entries": sorted(entries, key=lambda entry: entry["name"]), "returned_entries": len(entries)}
