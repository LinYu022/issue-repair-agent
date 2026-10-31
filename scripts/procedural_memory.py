"""Trusted local workflow extraction, lifecycle decisions, inspection and scoped search."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from domain.episodic_memory import EpisodeRef
from domain.procedural_memory import SkillDecision, SkillRef
from domain.semantic_memory import MemoryQuery
from runtime.execution_store import PostgresExecutionStore
from runtime.postgres import database_url
from runtime.skill_store import SkillStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["extract", "inspect", "search", "decide", "audit"])
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("new output required")
        value = json.loads(args.input.read_text(encoding="utf-8"))
        store = SkillStore(PostgresExecutionStore(database_url()))
        match args.action:
            case "extract":
                if set(value) - {"name", "sources", "parent"}:
                    raise ValueError("no arbitrary Skill body accepted")
                ref = store.extract(
                    args.user_id,
                    value["name"],
                    tuple(EpisodeRef.model_validate(r) for r in value["sources"]),
                    parent=SkillRef.model_validate(value["parent"]) if value.get("parent") else None,
                )
                result = ref.model_dump(mode="json")
            case "inspect":
                result = store.inspect(args.user_id, SkillRef.model_validate(value))
            case "search":
                result = store.search(args.user_id, MemoryQuery.model_validate(value))
            case "decide":
                result = store.decide(args.user_id, SkillDecision.model_validate(value))
            case "audit":
                if value:
                    raise ValueError("audit requires empty input")
                result = store.audit(args.user_id)
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n")
        print("procedural_memory_operation_recorded")
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print("SKILL OPERATION REFUSED: scope, version or evidence conflict; credentials omitted")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
