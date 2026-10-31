"""Whitelist export from a trusted snapshot, with independent descriptor-safe copies."""

import hashlib
from pathlib import Path
from uuid import UUID, uuid4

from domain.workspace_tools import TaskPackage, VisibleFile, VisibleTask
from domain.workspaces import WorkspaceIdentity, relative_parts
from tools.readonly_workspace import ReadOnlyWorkspace, WorkspaceRefused, directory_flags, open_root


def export_package(source: Path, task: VisibleTask, approved_files: dict[str, str]) -> TaskPackage:
    try:
        return _export_package(source, task, approved_files)
    except OSError:
        raise WorkspaceRefused("source_entry_unavailable_or_link") from None


def _export_package(source: Path, task: VisibleTask, approved_files: dict[str, str]) -> TaskPackage:
    """The operator supplies hashes after reviewing visibility; never infer allowed files."""
    import os

    if not approved_files or len(approved_files) > 512:
        raise WorkspaceRefused("explicit_file_inventory_required")
    files = []
    for path, expected in sorted(approved_files.items()):
        parts = relative_parts(path)
        descriptor = open_root(source)
        try:
            for part in parts[:-1]:
                child = os.open(part, directory_flags(), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            # Same bounded descriptor primitive, without giving a source path to the Agent.
            result = ReadOnlyWorkspace.read_file(descriptor, parts[-1], _ExportLimits())
        finally:
            os.close(descriptor)
        if result["sha256"] != expected:
            raise WorkspaceRefused("approved_source_hash_mismatch")
        files.append(VisibleFile(path=path, text=result["text"], sha256=expected))
    return TaskPackage(task=task, files=tuple(files))


class _ExportLimits:
    max_read_bytes = 262144


def materialize_package(parent: Path, package: TaskPackage, task_id: UUID) -> tuple[Path, WorkspaceIdentity]:
    """Fresh UUID destination; never reuse or overwrite a task directory."""
    import os

    parent_fd = open_root(parent)
    identity = WorkspaceIdentity(task_id, uuid4(), package.task.repo, package.task.base_commit)
    name = str(identity.workspace_id)
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        root = os.open(name, directory_flags(), dir_fd=parent_fd)
        try:
            for entry in package.files:
                parts = relative_parts(entry.path)
                descriptor = os.dup(root)
                try:
                    for part in parts[:-1]:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=descriptor)
                        except FileExistsError:
                            pass
                        child = os.open(part, directory_flags(), dir_fd=descriptor)
                        os.close(descriptor)
                        descriptor = child
                    output = os.open(
                        parts[-1],
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW"),
                        0o600,
                        dir_fd=descriptor,
                    )
                    with os.fdopen(output, "wb") as stream:
                        data = entry.text.encode("utf-8")
                        if hashlib.sha256(data).hexdigest() != entry.sha256:
                            raise WorkspaceRefused("package_integrity_mismatch")
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    os.close(descriptor)
        finally:
            os.close(root)
    finally:
        os.close(parent_fd)
    return parent / name, identity
