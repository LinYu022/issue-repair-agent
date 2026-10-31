"""Shared physical-line semantics for Python source locations and bounded quotes."""

from hashlib import sha256

from domain.repository_evidence import SourceExcerpt
from tools.readonly_workspace import WorkspaceRefused


def sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def physical_lines(text: str) -> list[str]:
    # Match Python's CR/LF line boundaries, not Unicode separators inside literals.
    lines = []
    start = index = 0
    while index < len(text):
        if text[index] in "\r\n":
            end = index + 1
            if text[index] == "\r" and end < len(text) and text[end] == "\n":
                end += 1
            lines.append(text[start:end])
            start = index = end
        else:
            index += 1
    if start < len(text):
        lines.append(text[start:])
    return lines


def excerpt(files: dict[str, str], path: str, start: int, end: int) -> SourceExcerpt:
    lines = physical_lines(files[path])
    if not 1 <= start <= end <= len(lines) or end - start >= 200:
        raise WorkspaceRefused("source_line_range_unavailable")
    text = "".join(lines[start - 1 : end])
    if len(text.encode()) > 8192:
        raise WorkspaceRefused("source_excerpt_limit")
    return SourceExcerpt(
        path=path,
        file_sha256=sha(files[path].encode()),
        start_line=start,
        end_line=end,
        text=text,
        text_sha256=sha(text.encode()),
    )


def context_excerpt(files: dict[str, str], path: str, line: int, radius: int) -> SourceExcerpt | None:
    lines = physical_lines(files[path])
    if not 1 <= line <= len(lines):
        return None
    try:
        return excerpt(files, path, max(1, line - radius), min(len(lines), line + radius))
    except WorkspaceRefused:
        return None  # Explicit omission; never truncate a quote while keeping its old line span.
