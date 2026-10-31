"""Read existing evidence without triggering model calls or fresh memory retrieval."""

import json

from domain.context import ContextBuildRecord
from domain.plans import document_hash


def memory_evidence(records, settings):
    paths = list((records.root / "context/builds").glob("*.json"))
    result = {
        "enabled": settings.memory_enabled,
        "sources": [],
        "scope": "recorded_prompt_sources_not_current_eligibility",
    }
    if not paths:
        return result
    sources = {}
    for path in paths:
        build = ContextBuildRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if build.summary_artifact:
            state = json.loads(records.artifacts.read_text(build.summary_artifact))
            items = state["memory"].get("items", [])
        else:
            items = []
            prompt = json.loads(records.artifacts.read_text(build.prompt_artifact))
            for message in prompt:
                try:
                    content = json.loads(message["content"])
                except (ValueError, TypeError):
                    continue
                if isinstance(content, dict) and "semantic_memory" in content:
                    items.extend(content["semantic_memory"].get("items", []))
        for item in items:
            sources[document_hash(item)] = item
    result["sources"] = list(sources.values())
    result["recorded_builds"] = len(paths)
    return result
