"""Small deterministic extractor and filters. No evaluator input and no model-based promotion."""

import re

from domain.plans import path_subset
from domain.semantic_memory import SemanticDocument

RULES = {
    "user_confirmation": ("preference", "confirmed_preference"),
    "static_document": ("convention", "document_assertion"),
    "visible_execution": ("command", "single_execution_observation"),
    "model_inference": ("hypothesis", "unverified_inference"),
}


def extract(source, memory_id, revision=1):
    kind, strength = RULES[source.origin]
    return SemanticDocument.model_validate(
        dict(
            memory_id=memory_id,
            revision=revision,
            user_id=source.user_id,
            source_id=source.source_id,
            kind=kind,
            strength=strength,
            key=source.key,
            value=source.quote,
            scope=source.scope,
            dependencies=source.dependencies,
        )
    )


def applies(document, query):
    scope = document.scope
    return (
        scope.repo == query.repo
        and query.branch in scope.branches
        and query.base_commit in scope.commits
        and path_subset(scope.paths, query.paths)
        and all(path_subset((path,), query.paths) for path in scope.watched_files)
        and (scope.environment_hash is None or scope.environment_hash == query.environment_hash)
        and (scope.workspace_version is None or scope.workspace_version == query.workspace_version)
        and all(
            path in query.file_hashes and query.file_hashes[path] == digest
            for path, digest in scope.watched_files.items()
        )
    )


def overlaps(a, b):
    return (
        a.repo == b.repo
        and bool(set(a.branches) & set(b.branches))
        and bool(set(a.commits) & set(b.commits))
        and any(path_subset((p,), (q,)) or path_subset((q,), (p,)) for p in a.paths for q in b.paths)
        and (a.environment_hash is None or b.environment_hash is None or a.environment_hash == b.environment_hash)
        and (a.workspace_version is None or b.workspace_version is None or a.workspace_version == b.workspace_version)
    )


def terms(value):
    return set(re.findall(r"[\w.-]+", value.casefold()))
