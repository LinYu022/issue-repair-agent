"""Bounded, non-executing Python AST definitions and conservative references.

References are syntax/lexical candidates, not a whole-program call graph. Dynamic
attributes, aliases, reassignments and inheritance must not become proven links.
"""

import ast
import platform
from dataclasses import dataclass, field
from typing import Any

from domain.workspaces import relative_parts

INDEX_VERSION = "python-ast-v1"
MAX_NODES = 150000


def digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()


def module_name(path: str) -> str:
    parts = path.rsplit(".", 1)[0].split("/")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@dataclass
class Scope:
    qualified: str
    kind: str
    parent: "Scope | None" = None
    bindings: dict[str, str | None] = field(default_factory=dict)
    unsupported: set[str] = field(default_factory=set)

    def bind(self, name: str, target: str | None):
        if name in self.bindings and self.bindings[name] != target:
            self.bindings[name] = None
        else:
            self.bindings[name] = target


class Bindings(ast.NodeVisitor):
    def __init__(self, scope: Scope, module: str, package: str):
        self.scope, self.module, self.package = scope, module, package

    def visit_FunctionDef(self, node):
        self.scope.bind(node.name, self.scope.qualified + "." + node.name)

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.scope.bind(node.id, None)

    def visit_Import(self, node):
        for alias in node.names:
            self.scope.bind(
                alias.asname or alias.name.split(".")[0], alias.name if alias.asname else alias.name.split(".")[0]
            )

    def visit_ImportFrom(self, node):
        parent = self.package.split(".") if self.package else []
        if node.level:
            if node.level > len(parent):
                base = ""
                self.scope.unsupported.add("relative_import_outside_inferred_package")
            else:
                base = ".".join(parent[: len(parent) - node.level + 1])
            prefix = ".".join(part for part in (base, node.module or "") if part)
            if node.level > len(parent):
                prefix = ""
        else:
            prefix = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                self.scope.unsupported.add("wildcard_import")
            else:
                self.scope.bind(
                    alias.asname or alias.name,
                    ".".join(part for part in (prefix, alias.name) if part) if prefix else None,
                )

    def visit_Global(self, node):
        for name in node.names:
            self.scope.bind(name, None)
        self.scope.unsupported.add("global_or_nonlocal_binding")

    visit_Nonlocal = visit_Global

    def visit_ExceptHandler(self, node):
        if node.name:
            self.scope.bind(node.name, None)
        self.generic_visit(node)

    # Comprehension and lambda bindings have separate scopes; do not taint outer names.
    def visit_Lambda(self, node):
        pass

    def visit_ListComp(self, node):
        for item in ast.walk(node):
            if isinstance(item, ast.NamedExpr):
                self.visit(item.target)

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def visit_MatchAs(self, node):
        if node.name:
            self.scope.bind(node.name, None)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.scope.bind(node.rest, None)
        self.generic_visit(node)


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        left = dotted(node.value)
        return None if left is None else left + "." + node.attr
    return None


class FileIndex(ast.NodeVisitor):
    def __init__(self, path: str, text: str, tree: ast.Module):
        self.path, self.text, self.module = path, text, module_name(path)
        self.file_hash = digest(text)
        self.package = (
            self.module if path.endswith(("/__init__.py", "/__init__.pyi")) else self.module.rpartition(".")[0]
        )
        self.scope = Scope(self.module, "module")
        self.definitions: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self.warnings: set[str] = set()
        self.parents = {id(child): parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        self.collect(tree.body)
        self.visit(tree)

    def collect(self, body):
        collector = Bindings(self.scope, self.module, self.package)
        for node in body:
            collector.visit(node)
        self.warnings.update(self.scope.unsupported)

    def reference(self, node, name, expression):
        target = None
        resolution = "unresolved_name"
        if expression:
            root, *rest = expression.split(".")
            scope: Scope | None = self.scope
            while scope:
                if scope.kind == "class" and self.scope.kind in {"function", "comprehension", "lambda"}:
                    scope = scope.parent
                    continue
                if root in scope.bindings:
                    binding = scope.bindings[root]
                    if binding:
                        target = ".".join([binding, *rest])
                        resolution = "lexical_or_import_candidate"
                    else:
                        resolution = "shadowed_or_runtime_binding"
                    break
                scope = scope.parent
            if root in {"self", "cls"} and rest:
                owner: Scope | None = self.scope
                while owner and owner.kind != "class":
                    owner = owner.parent
                if owner:
                    target = owner.qualified + "." + ".".join(rest)
                    resolution = "self_member_candidate"
            if isinstance(node, ast.Attribute) and target is None:
                resolution = "attribute_unresolved"
        else:
            resolution = "dynamic_attribute"
        parent = self.parents.get(id(node))
        self.references.append(
            {
                "path": self.path,
                "line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "column_utf8_bytes": node.col_offset,
                "name": name,
                "expression": expression,
                "scope": self.scope.qualified,
                "target": target,
                "resolution": resolution,
                "is_call": isinstance(parent, ast.Call) and parent.func is node,
                "file_sha256": self.file_hash,
            }
        )

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.reference(node, node.id, node.id)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load):
            self.reference(node, node.attr, dotted(node))
        # Keep inner expressions for dynamic calls, without duplicate attribute chains.
        base = node.value
        while isinstance(base, ast.Attribute):
            base = base.value
        self.visit(base)

    def visit_Call(self, node):
        expression = dotted(node.func)
        if expression in {
            "getattr",
            "setattr",
            "eval",
            "exec",
            "__import__",
            "globals",
            "locals",
            "importlib.import_module",
        }:
            self.warnings.add("dynamic_name_or_code_execution_present")
        self.generic_visit(node)

    def definition(self, node, kind):
        qualified = self.scope.qualified + "." + node.name
        self.definitions.append(
            {
                "symbol_id": digest(self.path + ":" + qualified + ":" + str(node.lineno) + ":" + self.file_hash),
                "name": node.name,
                "qualified_name": qualified,
                "kind": kind,
                "path": self.path,
                "line": node.lineno,
                "end_line": node.end_lineno or node.lineno,
                "column_utf8_bytes": node.col_offset,
                "file_sha256": self.file_hash,
            }
        )
        for decorator in node.decorator_list:
            self.visit(decorator)
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
        else:
            for value in (*node.args.defaults, *node.args.kw_defaults):
                if value is not None:
                    self.visit(value)
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if arg.annotation:
                    self.visit(arg.annotation)
            if node.returns:
                self.visit(node.returns)
        parent = self.scope
        self.scope = Scope(qualified, "class" if isinstance(node, ast.ClassDef) else "function", parent)
        self.collect(node.body)
        if not isinstance(node, ast.ClassDef):
            for arg in (
                *node.args.posonlyargs,
                *node.args.args,
                *node.args.kwonlyargs,
                node.args.vararg,
                node.args.kwarg,
            ):
                if arg:
                    self.scope.bind(arg.arg, None)
        for item in node.body:
            self.visit(item)
        self.scope = parent

    def visit_FunctionDef(self, node):
        self.definition(node, "function")

    def visit_AsyncFunctionDef(self, node):
        self.definition(node, "async_function")

    def visit_ClassDef(self, node):
        self.definition(node, "class")

    def visit_Lambda(self, node):
        for value in (*node.args.defaults, *node.args.kw_defaults):
            if value:
                self.visit(value)
        parent = self.scope
        self.scope = Scope(parent.qualified + ".<lambda>", "lambda", parent)
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, node.args.vararg, node.args.kwarg):
            if arg:
                self.scope.bind(arg.arg, None)
        Bindings(self.scope, self.module, self.package).visit(node.body)
        self.visit(node.body)
        self.scope = parent

    def comprehension(self, node):
        parent = self.scope
        for index, generator in enumerate(node.generators):
            self.visit(generator.iter)
            if index == 0:
                self.scope = Scope(parent.qualified + ".<comprehension>", "comprehension", parent)
            for target in ast.walk(generator.target):
                if isinstance(target, ast.Name):
                    self.scope.bind(target.id, None)
            for condition in generator.ifs:
                self.visit(condition)
        for field_name in ("elt", "key", "value"):
            value = getattr(node, field_name, None)
            if value is not None:
                self.visit(value)
        self.scope = parent

    visit_ListComp = comprehension
    visit_SetComp = comprehension
    visit_DictComp = comprehension
    visit_GeneratorExp = comprehension


def matches(qualified: str, query: str) -> bool:
    return qualified == query or qualified.endswith("." + query)


class PythonSymbolIndex:
    def __init__(self):
        self.key = None
        self.builds = 0
        self.definitions: list[dict[str, Any]] = []
        self.references: list[dict[str, Any]] = []
        self.diagnostics: list[dict[str, Any]] = []

    def ensure(self, files: dict[str, str], workspace_version: str):
        import json

        key = digest(
            json.dumps(
                {
                    "version": workspace_version,
                    "files": {p: digest(t) for p, t in sorted(files.items())},
                    "index": INDEX_VERSION,
                },
                sort_keys=True,
            )
        )
        if self.key == key:
            return "reused"
        definitions: list[dict[str, Any]] = []
        references: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        nodes = 0
        for path, text in sorted(files.items()):
            relative_parts(path)
            if not path.endswith((".py", ".pyi")):
                continue
            try:
                tree = ast.parse(text, filename=path, type_comments=True)
                count = sum(1 for _ in ast.walk(tree))
                if nodes + count > MAX_NODES:
                    diagnostics.append({"path": path, "reason": "ast_node_budget_exceeded"})
                    continue
                nodes += count
                indexed = FileIndex(path, text, tree)
                definitions.extend(indexed.definitions)
                references.extend(indexed.references)
                for warning in sorted(indexed.warnings):
                    diagnostics.append({"path": path, "reason": warning})
            except SyntaxError as error:
                diagnostics.append({"path": path, "reason": "syntax_error", "line": error.lineno})
            except (ValueError, RecursionError):
                diagnostics.append({"path": path, "reason": "ast_parse_or_depth_limit"})
        self.definitions, self.references, self.diagnostics = definitions, references, diagnostics
        self.key = key
        self.builds += 1
        return "rebuilt"

    def query(self, query: str, mode: str):
        if mode == "definitions":
            return [dict(item) for item in self.definitions if matches(item["qualified_name"], query)]
        return [
            dict(item)
            for item in self.references
            if (item["target"] and matches(item["target"], query))
            or (item["expression"] and matches(item["expression"], query))
            or ("." not in query and item["name"] == query)
        ]

    def metadata(self, version, cache_status):
        return {
            "index_id": self.key,
            "workspace_version": version,
            "index_version": INDEX_VERSION,
            "parser_python": platform.python_version(),
            "cache_status": cache_status,
            "diagnostics": self.diagnostics[:32],
            "diagnostic_count": len(self.diagnostics),
            "diagnostics_truncated": len(self.diagnostics) > 32,
            "limitations": [
                "syntactic_and_lexical_candidates_not_runtime_call_graph",
                "dynamic_attributes_imports_inheritance_and_rebinding_may_be_unresolved",
            ],
        }
