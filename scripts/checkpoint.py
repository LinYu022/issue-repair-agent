"""Trusted local committed-checkpoint inspection/export/GC, never automatic task resumption."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from runtime.checkpoint_archive import CheckpointArchive
from runtime.checkpoint_store import PostgresCheckpointStore
from runtime.execution_store import PostgresExecutionStore
from runtime.postgres import database_url


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["verify", "export-copy", "gc"])
    p.add_argument("--task-id", type=UUID, required=True)
    p.add_argument("--checkpoint-id", type=UUID)
    p.add_argument("--store", type=Path, required=True)
    p.add_argument("--destination-parent", type=Path)
    p.add_argument("--minimum-age-seconds", type=int, default=3600)
    args = p.parse_args()
    try:
        archive = CheckpointArchive(PostgresCheckpointStore(PostgresExecutionStore(database_url()), args.store))
        if args.action == "gc":
            result = archive.gc(args.task_id, minimum_age_seconds=args.minimum_age_seconds)
        elif args.checkpoint_id is None:
            raise ValueError("checkpoint_id_required")
        elif args.action == "export-copy":
            if args.destination_parent is None:
                raise ValueError("destination_parent_required")
            result = archive.export_copy(args.checkpoint_id, args.task_id, args.destination_parent)
        else:
            doc, workspace, graph = archive.load(args.checkpoint_id, args.task_id)
            result = {
                "checkpoint_id": str(doc.checkpoint_id),
                "content_version": workspace.content_version,
                "entries": len(workspace.entries),
                "authority_restored": False,
                "verified": True,
            }
        print(json.dumps(result))
        return 0
    except (ValueError, OSError):
        print(
            "CHECKPOINT OPERATION REFUSED: incomplete/corrupt/mismatched state or unavailable storage; credentials omitted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
