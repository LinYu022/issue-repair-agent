"""Trusted local history browsing and withdrawal. Raw trace import is host API only."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from domain.episodic_memory import BrowseEpisode, EpisodeQuery, EpisodeWithdrawal
from runtime.episode_store import EpisodeStore
from runtime.execution_store import PostgresExecutionStore
from runtime.postgres import database_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["search", "browse", "withdraw", "usage"])
    parser.add_argument("--user-id", type=UUID, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("new output required")
        value = json.loads(args.input.read_text(encoding="utf-8"))
        store = EpisodeStore(PostgresExecutionStore(database_url()))
        match args.action:
            case "search":
                result = store.search(args.user_id, EpisodeQuery.model_validate(value))
            case "browse":
                result = store.browse(
                    args.user_id,
                    EpisodeQuery.model_validate(value["query"]),
                    BrowseEpisode.model_validate(value["page"]),
                )
            case "withdraw":
                result = store.withdraw(
                    args.user_id, UUID(value.pop("episode_id")), EpisodeWithdrawal.model_validate(value)
                )
            case "usage":
                result = store.usage(args.user_id, UUID(value["task_id"]))
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print("episodic_memory_operation_recorded")
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print("EPISODIC MEMORY REFUSED: scope, state or evidence conflict; credentials omitted")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
