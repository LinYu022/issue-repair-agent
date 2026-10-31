"""Immutable byte objects with flush-before-publish. Workspace files remain bounded UTF-8 text."""

import hashlib
import os
import stat
from pathlib import Path
from uuid import uuid4

from domain.models import ArtifactRef


def fsync_directory(path: Path):
    if os.name != "posix":
        raise ValueError("checkpoint_publication_requires_linux_directory_fsync")
    fd = os.open(path, getattr(os, "O_DIRECTORY") | getattr(os, "O_NOFOLLOW"))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class SnapshotObjects:
    def __init__(self, root: Path):
        self.root = root

    def put(self, data: bytes):
        if len(data) > 16 * 1024 * 1024:
            raise ValueError("checkpoint_object_limit")
        missing = []
        parent = self.root
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in reversed(missing):
            fsync_directory(directory)
            fsync_directory(directory.parent)
        reference = ArtifactRef(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
        target = self.root / (reference.sha256 + ".blob")
        if target.exists():
            self.read(reference)
            return reference
        temporary = self.root / (".pending-" + uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW"), 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Link, rather than replace, never overwrites an existing immutable object.
            try:
                os.link(temporary, target)
            except FileExistsError:
                self.read(reference)
        finally:
            temporary.unlink(missing_ok=True)
        fsync_directory(self.root)
        self.read(reference)
        return reference

    def read(self, reference: ArtifactRef):
        path = self.root / (reference.sha256 + ".blob")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW"))
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != reference.size_bytes
                or before.st_size > 16 * 1024 * 1024
            ):
                raise ValueError("snapshot_object_not_regular_or_wrong_size")
            with os.fdopen(os.dup(fd), "rb") as stream:
                data = stream.read(16 * 1024 * 1024 + 1)
            after = os.fstat(fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("snapshot_object_changed_during_read")
        finally:
            os.close(fd)
        if len(data) != reference.size_bytes or hashlib.sha256(data).hexdigest() != reference.sha256:
            raise ValueError("snapshot_object_integrity_mismatch")
        return data
