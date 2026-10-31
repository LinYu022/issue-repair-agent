"""Source-only candidates seeded by reported symptoms, paths, stack frames and symbols."""

import re
from typing import Any

from domain.workspace_tools import InvestigateArgs, SymbolsArgs
from domain.workspaces import relative_parts
from tools.python_symbols import PythonSymbolIndex
from tools.source_spans import context_excerpt, physical_lines


def symbol_search(index: PythonSymbolIndex, files: dict[str, str], version: str, args: SymbolsArgs) -> dict[str, Any]:
    cached = index.ensure(files, version)
    hits = index.query(args.query, args.mode)
    returned = []
    for hit in sorted(hits, key=lambda hit: (hit["path"], hit["line"], hit["column_utf8_bytes"]))[: args.limit]:
        quote = context_excerpt(files, hit["path"], hit["line"], args.context_lines)
        returned.append(
            {
                **hit,
                "excerpt": None if quote is None else quote.model_dump(mode="json"),
                "excerpt_omitted": quote is None,
            }
        )
    return {
        **index.metadata(version, cached),
        "query": args.query,
        "mode": args.mode,
        "hits": returned,
        "total_matches": len(hits),
        "truncated": len(hits) > len(returned),
        "source_executed": False,
    }


def stack_frames(text: str):
    frames = []
    for match in re.finditer(r'File ["\']([^"\']+)["\'], line ([0-9]{1,7})(?:, in ([^\r\n]+))?', text):
        frames.append((match[1], int(match[2]), match[3]))
    # pytest's short traceback/file location, with no attempt to access absolute host paths.
    for match in re.finditer(r"(?m)^([^\s:]+\.py):([0-9]{1,7})(?::|\s)", text):
        frames.append((match[1], int(match[2]), None))
    return frames[:32]


def known_frame_path(path: str, files: dict[str, str]) -> str | None:
    if path.startswith("/workspace/"):
        path = path[len("/workspace/") :]
    try:
        relative_parts(path)
    except ValueError:
        return None
    return path if path in files else None


def investigate(index: PythonSymbolIndex, files: dict[str, str], version: str, args: InvestigateArgs) -> dict[str, Any]:
    cache = index.ensure(files, version)
    candidates: dict[tuple[str, int], dict[str, Any]] = {}
    diagnostics = []

    def add(path, line, reason, weight, symbol=None):
        if path not in files or not 1 <= line <= len(physical_lines(files[path])):
            return
        key = (path, line)
        candidate = candidates.setdefault(
            key, {"path": path, "line": line, "symbol": symbol, "score": 0, "reasons": []}
        )
        if reason not in candidate["reasons"]:
            candidate["score"] += weight
            candidate["reasons"].append(reason)
        if symbol and not candidate["symbol"]:
            candidate["symbol"] = symbol

    extracted = re.findall(r"\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\b", args.issue)
    queries = list(dict.fromkeys([*args.symbols, *extracted]))[:32]
    for query in queries:
        for definition in index.query(query, "definitions"):
            add(definition["path"], definition["line"], "exact_symbol_syntax", 60, definition["qualified_name"])
    paths = list(args.file_paths)
    paths.extend(match.group(0) for match in re.finditer(r"[A-Za-z0-9_./-]+\.py\b", args.issue))
    for path in dict.fromkeys(paths):
        if path in files:
            add(path, 1, "reported_file_path", 20)
    for entry in args.test_entries:
        parts = entry.split("::")
        path = parts[0]
        if path not in files:
            diagnostics.append({"kind": "test_entry_outside_visible_inventory"})
            continue
        if len(parts) > 1:
            query = ".".join(parts[1:])
            found = [item for item in index.query(query, "definitions") if item["path"] == path]
            for item in found:
                add(path, item["line"], "reported_test_entry_matches_ast", 40, item["qualified_name"])
            if not found:
                add(path, 1, "reported_test_entry_unresolved", 10)
        else:
            add(path, 1, "reported_test_file", 30)
    for reported, line, _function in stack_frames(args.traceback):
        frame_path = known_frame_path(reported, files)
        if frame_path is None:
            diagnostics.append({"kind": "traceback_path_not_in_visible_workspace"})
            continue
        if not 1 <= line <= len(physical_lines(files[frame_path])):
            diagnostics.append({"kind": "traceback_line_out_of_range", "path": frame_path})
            continue
        add(frame_path, line, "reported_traceback_frame", 80)
        enclosing = [
            item
            for item in index.definitions
            if item["path"] == frame_path and item["line"] <= line <= item["end_line"]
        ]
        if enclosing:
            enclosing.sort(key=lambda item: item["end_line"] - item["line"])
            item = enclosing[0]
            add(frame_path, item["line"], "ast_enclosing_reported_frame", 45, item["qualified_name"])
    errors = list(
        dict.fromkeys(re.findall(r"\b[A-Z][A-Za-z0-9_]*(?:Error|Exception)\b", args.traceback + "\n" + args.issue))
    )[:8]
    for error in errors:
        for ref in index.query(error, "references"):
            add(ref["path"], ref["line"], "reported_exception_name_matches_syntax", 10, ref["scope"])
    # Generic issue terms are low-weight hints and never become asserted root causes.
    words = list(dict.fromkeys(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{2,}\b", args.issue)))[:24]
    for word in words:
        for definition in index.query(word, "definitions"):
            add(
                definition["path"],
                definition["line"],
                "issue_identifier_matches_definition",
                15,
                definition["qualified_name"],
            )
    ordered = sorted(candidates.values(), key=lambda item: (-item["score"], item["path"], item["line"]))
    selected = []
    for candidate in ordered[: args.limit]:
        quote = context_excerpt(files, candidate["path"], candidate["line"], args.context_lines)
        selected.append(
            {
                **candidate,
                "excerpt": None if quote is None else quote.model_dump(mode="json"),
                "excerpt_omitted": quote is None,
                "status": "candidate_not_verified_root_cause",
            }
        )
    return {
        **index.metadata(version, cache),
        "candidates": selected,
        "candidate_count": len(ordered),
        "truncated": len(ordered) > len(selected),
        "input_diagnostics": diagnostics,
        "ranking": "deterministic_hint_weights_not_probability",
        "source_executed": False,
        "input_provenance": "reported_issue_paths_stack_and_tests_unverified",
        "input_bytes": len(args.model_dump_json().encode()),
        "input_digest": __import__("hashlib").sha256(args.model_dump_json().encode()).hexdigest(),
    }
