"""One durable asynchronous candidate job. This worker cannot promote knowledge."""

import argparse
import json
import os
from uuid import UUID, uuid4

from runtime.execution_store import PostgresExecutionStore
from runtime.postgres import database_url
from runtime.semantic_store import SemanticStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lease-seconds", type=int, default=15)
    parser.add_argument("--job-id", type=UUID, help="Optionally claim only this already registered job")
    parser.add_argument(
        "--claim-only", action="store_true", help="Exit after persisting ownership, for controller fault probes"
    )
    parser.add_argument("--crash-before-commit", action="store_true", help="Controller-only semantic crash fixture")
    args = parser.parse_args()
    try:
        store = SemanticStore(PostgresExecutionStore(database_url()))
        lease = store.claim(uuid4(), seconds=args.lease_seconds, job_id=args.job_id)
        if lease is None:
            print(json.dumps({"job": "idle"}))
            return 0
        if args.claim_only:
            print(json.dumps({"lease": lease}, default=str))
            return 0

        def fault(stage):
            if args.crash_before_commit:
                os._exit(91)

        result = store.finish_candidate(lease, fault=fault)
        print(json.dumps({"job": "candidate_created", "memory_id": result["memory_id"], "state": result["state"]}))
        return 0
    except (ValueError, OSError):
        print(json.dumps({"job": "refused", "credentials_disclosed": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
