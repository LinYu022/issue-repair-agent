"""Trusted local operator: inspect or cancel persistent tasks; no worker/authority impersonation."""

import argparse
import json
from uuid import UUID

from runtime.execution_store import PostgresExecutionStore
from runtime.postgres import database_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["show", "cancel", "pending"])
    parser.add_argument("--task-id", type=UUID)
    args = parser.parse_args()
    try:
        store = PostgresExecutionStore(database_url())
        if args.action == "pending":
            value = {"pending_tasks": [str(i) for i in store.pending()]}
        else:
            if args.task_id is None:
                raise ValueError("task_id_required")
            if args.action == "cancel":
                store.cancel(args.task_id)
            row = store.read(args.task_id)
            value = {
                k: row[k]
                for k in [
                    "task_id",
                    "worker_id",
                    "lease_epoch",
                    "lease_until",
                    "cancelled",
                    "blocked_reason",
                    "active_call_id",
                    "deadline",
                ]
            }
        print(json.dumps(value, default=str))
        return 0
    except (ValueError, OSError):
        print("EXECUTION OPERATION REFUSED: inspect ownership/schema/connection; credentials omitted")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
