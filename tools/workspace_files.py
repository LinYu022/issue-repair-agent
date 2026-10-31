"""Bounded Linux descriptor filesystem operations, with whole-workspace versions."""

import difflib
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any
from uuid import uuid4

from domain.repository_evidence import SourceExcerpt
from domain.workspace_tools import DiffArgs, EditArgs, InvestigateArgs, ListArgs, ReadArgs, SearchArgs, SymbolsArgs
from domain.workspaces import relative_parts
from tools.readonly_workspace import ReadOnlyWorkspace, WorkspaceRefused, directory_flags, open_root, stamp


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def workspace_version(files: dict[str, str]) -> str:
    return sha(json.dumps({name: sha(text.encode()) for name, text in sorted(files.items())}, sort_keys=True).encode())


def git_path(name: str) -> str:
    # Git C quoting uses octal UTF-8 bytes rather than JSON's Unicode escapes.
    encoded = "".join(
        chr(byte)
        if 32 <= byte < 127 and byte not in {34, 92}
        else '\\"'
        if byte == 34
        else "\\\\"
        if byte == 92
        else f"\\{byte:03o}"
        for byte in name.encode()
    )
    return '"' + encoded + '"'


def rename_noreplace(source_fd, source, target_fd, target):
    import ctypes
    import errno

    library = ctypes.CDLL(None, use_errno=True)
    rename = getattr(library, "renameat2", None)
    if rename is None:
        raise WorkspaceRefused("atomic_rename_noreplace_unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(source_fd, os.fsencode(source), target_fd, os.fsencode(target), 1) != 0:
        code = ctypes.get_errno()
        if code == errno.EEXIST:
            raise WorkspaceRefused("rename_target_must_be_absent")
        raise OSError(code, "bounded_atomic_rename_failed")


class FileLimits:
    max_read_bytes = 262144


class WorkspaceFiles:
    def __init__(self, root: Path):
        self.root = root
        descriptor = open_root(root)
        try:
            status = os.fstat(descriptor)
            self.identity = status.st_dev, status.st_ino
        finally:
            os.close(descriptor)
        self.baseline = self.snapshot()
        from tools.python_symbols import PythonSymbolIndex

        self.symbols = PythonSymbolIndex()

    def root_fd(self):
        descriptor = open_root(self.root)
        status = os.fstat(descriptor)
        if self.identity != (status.st_dev, status.st_ino):
            os.close(descriptor)
            raise WorkspaceRefused("workspace_root_replaced")
        return descriptor

    def snapshot(self) -> dict[str, str]:
        files: dict[str, str] = {}
        entries = 0
        total = 0

        def walk(descriptor, prefix, depth):
            nonlocal entries, total
            if depth > 16:
                raise WorkspaceRefused("directory_depth_limit")
            before = os.fstat(descriptor)
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > 1024:
                        raise WorkspaceRefused("workspace_entry_limit")
                    path = prefix + entry.name
                    relative_parts(path)
                    status = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(status.st_mode):
                        child = os.open(entry.name, directory_flags(), dir_fd=descriptor)
                        try:
                            walk(child, path + "/", depth + 1)
                        finally:
                            os.close(child)
                    else:
                        result = ReadOnlyWorkspace.read_file(descriptor, entry.name, FileLimits())
                        total += result["size_bytes"]
                        if len(files) >= 512 or total > 4 * 1024 * 1024:
                            raise WorkspaceRefused("workspace_size_limit")
                        files[path] = result["text"]
            if stamp(before) != stamp(os.fstat(descriptor)):
                raise WorkspaceRefused("directory_changed_during_snapshot")

        descriptor = self.root_fd()
        try:
            try:
                walk(descriptor, "", 0)
            except OSError:
                raise WorkspaceRefused("workspace_entry_unavailable_or_link") from None
        finally:
            os.close(descriptor)
        if len({path.casefold() for path in files}) != len(files):
            raise WorkspaceRefused("case_collision")
        return files

    def edit(self, args: EditArgs, files: dict[str, str]):
        old = files.get(args.path)
        if (None if old is None else sha(old.encode())) != args.expected_sha256:
            raise WorkspaceRefused("file_precondition_conflict")
        if args.text is None and old is None:
            raise WorkspaceRefused("cannot_delete_missing_file")
        proposed = dict(files)
        if args.text is None:
            proposed.pop(args.path)
            if args.destination is not None:
                assert old is not None
                if args.destination in files:
                    raise WorkspaceRefused("rename_target_must_be_absent")
                proposed[args.destination] = old
        else:
            proposed[args.path] = args.text
        if len(proposed) > 512 or sum(len(text.encode()) for text in proposed.values()) > 4 * 1024 * 1024:
            raise WorkspaceRefused("workspace_size_limit")
        if args.text is not None and len(args.text.encode()) > 262144:
            raise WorkspaceRefused("file_size_limit")
        if len({path.casefold() for path in proposed}) != len(proposed):
            raise WorkspaceRefused("case_collision")
        parts = relative_parts(args.path)
        descriptor = self.root_fd()
        temporary = ".edit-" + uuid4().hex
        try:
            for part in parts[:-1]:
                # Parent directories must already exist; no implicit path expansion.
                child = os.open(part, directory_flags(), dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            try:
                observed = ReadOnlyWorkspace.read_file(descriptor, parts[-1], FileLimits())
            except FileNotFoundError:
                observed = None
            if (None if observed is None else observed["sha256"]) != args.expected_sha256:
                raise WorkspaceRefused("file_precondition_conflict")
            if args.text is None:
                if args.destination is not None:
                    target = relative_parts(args.destination)
                    destination_fd = self.root_fd()
                    try:
                        for part in target[:-1]:
                            child = os.open(part, directory_flags(), dir_fd=destination_fd)
                            os.close(destination_fd)
                            destination_fd = child
                        try:
                            os.stat(target[-1], dir_fd=destination_fd, follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            raise WorkspaceRefused("rename_target_must_be_absent")
                        rename_noreplace(descriptor, parts[-1], destination_fd, target[-1])
                        os.fsync(destination_fd)
                    finally:
                        os.close(destination_fd)
                else:
                    os.unlink(parts[-1], dir_fd=descriptor)
            else:
                fd = os.open(
                    temporary,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW"),
                    0o600,
                    dir_fd=descriptor,
                )
                try:
                    with os.fdopen(fd, "wb") as stream:
                        mode = (
                            0o600
                            if observed is None
                            else stat.S_IMODE(os.stat(parts[-1], dir_fd=descriptor, follow_symlinks=False).st_mode)
                        )
                        if mode & 0o7000:
                            raise WorkspaceRefused("unsupported_special_file_permissions")
                        getattr(os, "fchmod")(stream.fileno(), mode)
                        stream.write(args.text.encode("utf-8"))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, parts[-1], src_dir_fd=descriptor, dst_dir_fd=descriptor)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=descriptor)
                    except FileNotFoundError:
                        pass
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return {
            "changed": old != args.text,
            "sha256": None if args.text is None else sha(args.text.encode()),
            **({"renamed_to": args.destination} if args.destination is not None else {}),
        }

    def execute(self, args, files: dict[str, str], max_bytes: int, *, version: str | None = None):
        result: dict[str, Any]
        quote: SourceExcerpt | None
        path = args.path
        selected = {
            name: text for name, text in files.items() if path == "." or name == path or name.startswith(path + "/")
        }
        if isinstance(args, ReadArgs):
            if path not in files:
                raise WorkspaceRefused("workspace_file_missing")
            from tools.source_spans import context_excerpt, excerpt, physical_lines

            lines = physical_lines(files[path])
            if args.start_line is not None:
                assert args.end_line is not None
                quote = excerpt(files, path, args.start_line, args.end_line)
                text = quote.text
            else:
                text = files[path]
                quote = context_excerpt(files, path, 1, 1) if lines else None
            result = {
                "text": text,
                "sha256": sha(files[path].encode()),
                "start_line": args.start_line or (1 if lines else None),
                "end_line": args.end_line or (len(lines) if lines else None),
                "file_line_count": len(lines),
                "excerpt": None if quote is None else quote.model_dump(mode="json"),
            }
        elif isinstance(args, ListArgs):
            if path in files or (path != "." and not selected):
                raise WorkspaceRefused("workspace_directory_missing")
            prefix = "" if path == "." else path + "/"
            result = {"entries": sorted({name[len(prefix) :].split("/")[0] for name in selected})}
        elif isinstance(args, SearchArgs):
            from tools.source_spans import context_excerpt, physical_lines

            matches = []
            for name, text in sorted(selected.items()):
                for number, line in enumerate(physical_lines(text), 1):
                    if args.query in line:
                        quote = context_excerpt(files, name, number, 0)
                        matches.append(
                            {
                                "path": name,
                                "line": number,
                                "text": line.rstrip("\r\n"),
                                "excerpt": None if quote is None else quote.model_dump(mode="json"),
                            }
                        )
                        if len(matches) > 128:
                            raise WorkspaceRefused("search_match_limit")
            result = {"matches": matches}
        elif isinstance(args, SymbolsArgs):
            from tools.repository_search import symbol_search

            result = symbol_search(self.symbols, selected, version or workspace_version(files), args)
        elif isinstance(args, InvestigateArgs):
            from tools.repository_search import investigate

            result = investigate(self.symbols, selected, version or workspace_version(files), args)
        elif isinstance(args, EditArgs):
            result = self.edit(args, files)
        elif isinstance(args, DiffArgs):
            patches = []
            for name in sorted(set(self.baseline) | set(files)):
                if path != "." and name != path and not name.startswith(path + "/"):
                    continue
                before, after = self.baseline.get(name), files.get(name)
                if before == after:
                    continue
                header = f"diff --git {git_path('a/' + name)} {git_path('b/' + name)}\n"
                if before is None:
                    header += "new file mode 100644\n"
                elif after is None:
                    header += "deleted file mode 100644\n"
                lines = list(
                    difflib.unified_diff(
                        [] if before is None else before.splitlines(keepends=True),
                        [] if after is None else after.splitlines(keepends=True),
                        "/dev/null" if before is None else git_path("a/" + name),
                        "/dev/null" if after is None else git_path("b/" + name),
                    )
                )
                # difflib omits Git's explicit missing-newline marker; include it per changed line.
                patches.append(
                    header
                    + "".join(
                        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines
                    )
                )
            result = {"patch": "".join(patches), "base_version": workspace_version(self.baseline)}
        else:
            raise WorkspaceRefused("unsupported_file_operation")
        if len(json.dumps(result, ensure_ascii=False).encode()) > max_bytes:
            raise WorkspaceRefused("tool_output_limit")
        return result
