"""Trusted local memory-owner CLI. Keep the user binding outside task/model inputs."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from domain.semantic_memory import EnvironmentChange, MemoryAmend, MemoryQuery, MemoryReview, UserConfirmation
from runtime.execution_store import PostgresExecutionStore
from runtime.postgres import database_url
from runtime.semantic_ingest import SemanticIngestor
from runtime.semantic_store import SemanticStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["query", "candidates", "read", "review", "amend", "invalidate", "revoke-source", "confirm-preference"],
    )
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("new output required")
        value = json.loads(args.input.read_text(encoding="utf-8"))
        store = SemanticStore(PostgresExecutionStore(database_url()))
        match args.action:
            case "query":
                result = store.retrieve(args.user_id, MemoryQuery.model_validate(value))
            case "candidates":
                result = store.candidates(args.user_id)
            case "read":
                result = store.read(args.user_id, UUID(value["memory_id"]))
            case "review":
                result = store.review(args.user_id, MemoryReview.model_validate(value))
            case "amend":
                result = store.amend(args.user_id, UUID(value.pop("memory_id")), MemoryAmend.model_validate(value))
            case "invalidate":
                result = store.invalidate(args.user_id, EnvironmentChange.model_validate(value))
            case "revoke-source":
                result = store.revoke_source(
                    args.user_id, UUID(value["source_id"]), UUID(value["request_id"]), value["reason"]
                )
            case "confirm-preference":
                source = UserConfirmation.model_validate(value)
                SemanticIngestor(store).confirmation(args.user_id, source)
                result = {
                    "source_id": str(source.source_id),
                    "job_id": str(store.enqueue(args.user_id, source.source_id)),
                }
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print("semantic_memory_operation_recorded")
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print("SEMANTIC MEMORY REFUSED: scope, evidence, identity or schema conflict; credentials omitted")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
