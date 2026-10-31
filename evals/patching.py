"""Strict UTF-8 unified patch application, without running Git filters or hooks."""

import re
from dataclasses import dataclass

from domain.workspace_tools import TaskPackage, VisibleFile
from domain.workspaces import relative_parts
from tools.workspace_files import sha, workspace_version


@dataclass(frozen=True)
class AppliedPatch:
    files: dict[str, str]
    changed_paths: tuple[str, ...]
    effective_hash: str


def header_path(line: str, prefix: str) -> str | None:
    value = line[len(prefix) :].rstrip("\n")
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        if not value.endswith('"'):
            raise ValueError("unterminated_patch_path")
        encoded = bytearray()
        remaining = value[1:-1]
        while remaining:
            if remaining.startswith("\\"):
                match = re.match(r"\\([0-7]{3}|[\\\"])", remaining)
                if not match:
                    raise ValueError("unsupported_patch_path_escape")
                token = match[1]
                encoded.extend(bytes([int(token, 8)]) if len(token) == 3 else token.encode())
                remaining = remaining[len(match[0]) :]
            else:
                encoded.extend(remaining[0].encode())
                remaining = remaining[1:]
        value = encoded.decode("utf-8")
    if not value.startswith(("a/", "b/")) or "\t" in value:
        raise ValueError("canonical_patch_path_required")
    path = value[2:]
    relative_parts(path)
    return path


def apply_patch(package: TaskPackage, patch: str) -> AppliedPatch:
    if len(patch.encode()) > 1048576 or "\r" in patch or "\0" in patch:
        raise ValueError("patch_encoding_or_size")
    files = {entry.path: entry.text for entry in package.files}
    if not patch.strip():
        return AppliedPatch(files, (), workspace_version(files))
    lines = patch.splitlines(keepends=True)
    index, seen, changed = 0, set(), []
    while index < len(lines):
        while index < len(lines) and lines[index].startswith(
            ("diff --git ", "index ", "new file mode 100644", "deleted file mode 100644")
        ):
            index += 1
        if index + 1 >= len(lines) or not lines[index].startswith("--- ") or not lines[index + 1].startswith("+++ "):
            raise ValueError("unified_file_headers_required")
        before = header_path(lines[index], "--- ")
        after = header_path(lines[index + 1], "+++ ")
        if (before is None and after is None) or (before and after and before != after):
            raise ValueError("rename_or_missing_paths_refused")
        path = before or after
        assert path is not None
        if path in seen or (before is None and path in files) or (before is not None and path not in files):
            raise ValueError("patch_file_precondition")
        seen.add(path)
        source = files.get(path, "").splitlines(keepends=True)
        output, cursor, hunks = [], 0, 0
        index += 2
        while index < len(lines) and lines[index].startswith("@@ "):
            match = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n", lines[index])
            if not match:
                raise ValueError("invalid_hunk_header")
            start_old, count_old = int(match[1]), int(match[2] or 1)
            start_new, count_new = int(match[3]), int(match[4] or 1)
            position = start_old - 1 if count_old else start_old
            if position < cursor or position > len(source):
                raise ValueError("hunk_position_conflict")
            output.extend(source[cursor:position])
            cursor = position
            if len(output) != (start_new - 1 if count_new else start_new):
                raise ValueError("new_hunk_position_conflict")
            index += 1
            consumed = produced = 0
            while index < len(lines) and (consumed < count_old or produced < count_new):
                line = lines[index]
                if not line or line[0] not in " +-":
                    raise ValueError("hunk_line_required")
                marker, content = line[0], line[1:]
                index += 1
                if index < len(lines) and lines[index] == "\\ No newline at end of file\n":
                    if not content.endswith("\n"):
                        raise ValueError("invalid_no_newline_marker")
                    content = content[:-1]
                    index += 1
                if marker in " -":
                    if cursor >= len(source) or source[cursor] != content:
                        raise ValueError("hunk_context_conflict")
                    cursor += 1
                    consumed += 1
                if marker in " +":
                    output.append(content)
                    produced += 1
                if consumed > count_old or produced > count_new:
                    raise ValueError("hunk_count_conflict")
            hunks += 1
        if not hunks:
            raise ValueError("at_least_one_hunk_required")
        output.extend(source[cursor:])
        text = "".join(output)
        if after is None:
            if text:
                raise ValueError("delete_requires_empty_result")
            del files[path]
        else:
            files[path] = text
        if package_file_text(package, path) != files.get(path):
            changed.append(path)
    # Revalidate inventory, text sizes and collisions after application.
    TaskPackage(
        task=package.task, files=tuple(VisibleFile(path=p, text=t, sha256=sha(t.encode())) for p, t in files.items())
    )
    return AppliedPatch(files, tuple(changed), workspace_version(files))


def package_file_text(package: TaskPackage, path: str) -> str | None:
    return next((entry.text for entry in package.files if entry.path == path), None)
