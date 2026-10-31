"""Trusted local operator CLI: inspect exact review, record decision, then restore at a decision boundary."""

import argparse
import json
from pathlib import Path
from uuid import UUID

from domain.plans import ControlRequest
from domain.recovery import RecoveryDecision, RecoveryRequest
from runtime.execution_store import PostgresExecutionStore
from runtime.postgres import database_url
from runtime.recovery import RecoveryService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "decision", "apply", "quiesce"])
    parser.add_argument("--input", type=Path, required=True, help="operator-owned JSON request, decision or identity")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ValueError("recovery_output_must_be_new")
        value = json.loads(args.input.read_text(encoding="utf-8"))
        service = RecoveryService(PostgresExecutionStore(database_url()))
        if args.action == "inspect":
            review = service.inspect(RecoveryRequest.model_validate(value))
            from domain.plans import document_hash

            result = {
                "review": review.model_dump(mode="json"),
                "review_hash": document_hash(review),
                "plan_hash": None if review.new_plan is None else document_hash(review.new_plan),
            }
        elif args.action == "decision":
            service.store.approve(RecoveryDecision.model_validate(value))
            result = {"decision_recorded": True}
        elif args.action == "quiesce":
            result = service.quiesce(UUID(value["task_id"]), action=value["action"])
        else:
            restored = service.apply(UUID(value["recovery_id"]))
            if isinstance(restored, dict):
                result = restored
            else:
                # CLI creates a durable reviewed pause. A hosting application may instead keep
                # the returned original RepairLoop in memory and call run(adapter).
                try:
                    restored.controller.control(
                        ControlRequest(
                            action="pause",
                            expected_revision_id=restored.controller.current.revision_id,
                            reason="Local recovery CLI verified restoration; host has not attached a model adapter",
                        )
                    )
                    from runtime.checkpoints import JointCheckpoints

                    checkpoint = JointCheckpoints(restored).create()
                    result = {
                        **service.store.run(UUID(value["recovery_id"]))["result"],
                        "paused_checkpoint": checkpoint,
                    }
                finally:
                    restored.session.durable.close()
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print("recovery_operation_recorded")
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print("RECOVERY REFUSED: inspect local review and identity; credentials omitted")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
