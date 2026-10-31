"""Persistent exact-review approvals, ownership rebind and recovery audit, separate from snapshots."""

from datetime import datetime, timezone

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from domain.execution import ExecutionRefused, Lease
from domain.plans import document_hash
from domain.recovery import RecoveryReview
from runtime.checkpoint_store import migrate_checkpoints
from runtime.execution_store import SQL_ROOT


def migrate_recovery(connection, direction="up"):
    if direction not in {"up", "down", "status"}:
        raise ExecutionRefused("recovery_migration_direction")
    checksum = document_hash(
        {k: (SQL_ROOT / f"0004_recovery.{k}.sql").read_text(encoding="utf-8") for k in ["up", "down"]}
    )
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(2026102812)")
        if migrate_checkpoints(connection, "status") != 1:
            raise ExecutionRefused("checkpoint_schema_required")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS public.agent_recovery_schema(version integer PRIMARY KEY,checksum text NOT NULL)"
        )
        rows = connection.execute("SELECT version,checksum FROM public.agent_recovery_schema").fetchall()
        if rows and rows != [(1, checksum)]:
            raise ExecutionRefused("recovery_migration_checksum")
        if direction == "up" and not rows:
            connection.execute((SQL_ROOT / "0004_recovery.up.sql").read_text())
            connection.execute("INSERT INTO public.agent_recovery_schema VALUES(1,%s)", (checksum,))
            return 1
        if direction == "down" and rows:
            connection.execute("LOCK TABLE public.agent_recovery_runtime IN ACCESS EXCLUSIVE MODE")
            if connection.execute("SELECT EXISTS(SELECT 1 FROM public.agent_recovery_runtime)").fetchone()[0]:
                raise ExecutionRefused("recovery_downgrade_data_exists")
            connection.execute((SQL_ROOT / "0004_recovery.down.sql").read_text())
            connection.execute("DELETE FROM public.agent_recovery_schema")
            return 0
        return 1 if rows else 0


class RecoveryStore:
    def __init__(self, execution):
        self.execution = execution

    def register(self, loop, spec, parent):
        owner = loop.session.durable
        if owner is None:
            raise ExecutionRefused("recovery_requires_persistent_owner")
        owner.check()
        with self.execution.connection() as c:
            c.execute(
                "INSERT INTO public.agent_recovery_runtime(task_id,workspace_path,trace_path,checkpoint_path,lock_path,recovery_parent,spec,spec_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    owner.task_id,
                    str(owner.workspace),
                    str(loop.session.records.root.resolve()),
                    str(owner.snapshot_root.resolve()),
                    str(owner.lock_path),
                    str(parent.resolve()),
                    Jsonb(spec),
                    document_hash(spec),
                ),
            )
        loop.recovery_info = {"enabled": True, "restored": False, "original_authority_reused": False}

    def runtime(self, task_id):
        with self.execution.connection() as c:
            c.row_factory = dict_row
            row = c.execute("SELECT * FROM public.agent_recovery_runtime WHERE task_id=%s", (task_id,)).fetchone()
            if row is None or document_hash(row["spec"]) != row["spec_hash"]:
                raise ExecutionRefused("recovery_runtime_missing_or_changed")
            return row

    def calls(self, task_id):
        with self.execution.connection() as c:
            c.row_factory = dict_row
            rows = c.execute(
                "SELECT * FROM public.agent_execution_calls WHERE task_id=%s ORDER BY created_at,call_id", (task_id,)
            ).fetchall()
            for row in rows:
                if (
                    row["intent_hash"] != document_hash(row["intent"])
                    or row["result"] is not None
                    and row["result_hash"] != document_hash(row["result"])
                ):
                    raise ExecutionRefused("recovery_call_integrity")
            return rows

    def begin(self, request):
        with self.execution.locked(request.task_id) as (c, row):
            old = c.execute(
                "SELECT * FROM public.agent_recovery_runs WHERE recovery_id=%s", (request.recovery_id,)
            ).fetchone()
            if old:
                if old["request_hash"] != document_hash(request):
                    raise ExecutionRefused("recovery_id_payload_conflict")
                return old
            c.execute(
                "INSERT INTO public.agent_recovery_runs(recovery_id,task_id,request,request_hash,state) VALUES(%s,%s,%s,%s,'INSPECTING')",
                (request.recovery_id, request.task_id, Jsonb(request.model_dump(mode="json")), document_hash(request)),
            )
        return self.run(request.recovery_id)

    def run(self, recovery_id):
        with self.execution.connection() as c:
            c.row_factory = dict_row
            row = c.execute("SELECT * FROM public.agent_recovery_runs WHERE recovery_id=%s", (recovery_id,)).fetchone()
            if row is None:
                raise ExecutionRefused("recovery_request_missing")
            from domain.recovery import RecoveryRequest

            if row["request_hash"] != document_hash(RecoveryRequest.model_validate(row["request"])):
                raise ExecutionRefused("recovery_request_integrity")
            if row["review"] is not None and row["review_hash"] != document_hash(
                RecoveryReview.model_validate(row["review"])
            ):
                raise ExecutionRefused("recovery_review_integrity")
            if row["result"] is not None and row["result_hash"] != document_hash(row["result"]):
                raise ExecutionRefused("recovery_result_integrity")
            return row

    def event(self, recovery_id, value):
        with self.execution.connection() as c:
            c.execute(
                "SELECT recovery_id FROM public.agent_recovery_runs WHERE recovery_id=%s FOR UPDATE", (recovery_id,)
            )
            sequence = c.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM public.agent_recovery_events WHERE recovery_id=%s",
                (recovery_id,),
            ).fetchone()[0]
            c.execute(
                "INSERT INTO public.agent_recovery_events VALUES(%s,%s,%s,%s)",
                (recovery_id, sequence, Jsonb(value), document_hash(value)),
            )

    def review(self, value: RecoveryReview):
        with self.execution.connection() as c:
            updated = c.execute(
                "UPDATE public.agent_recovery_runs SET state=%s,review=%s,review_hash=%s WHERE recovery_id=%s AND state='INSPECTING'",
                (value.status, Jsonb(value.model_dump(mode="json")), document_hash(value), value.request.recovery_id),
            )
            if updated.rowcount != 1:
                return RecoveryReview.model_validate(self.run(value.request.recovery_id)["review"])
        self.event(
            value.request.recovery_id, {"stage": "inspection_complete", "status": value.status, "reason": value.reason}
        )
        return value

    def approve(self, decision):
        row = self.run(decision.recovery_id)
        if row["state"] != "READY" or row["review_hash"] != decision.review_hash:
            raise ExecutionRefused("approval_not_for_current_recovery_review")
        review = RecoveryReview.model_validate(row["review"])
        if review.new_plan is None or document_hash(review.new_plan) != decision.plan_hash:
            raise ExecutionRefused("recovery_plan_hash_mismatch")
        if decision.expires_at <= datetime.now(timezone.utc):
            raise ExecutionRefused("recovery_decision_expired")
        with self.execution.connection() as c:
            changed = c.execute(
                "UPDATE public.agent_recovery_runs SET state=%s,decision=%s WHERE recovery_id=%s AND state='READY' AND review_hash=%s RETURNING recovery_id",
                (
                    "APPROVED" if decision.decision == "approve" else "SAFE_STOPPED",
                    Jsonb(decision.model_dump(mode="json")),
                    decision.recovery_id,
                    decision.review_hash,
                ),
            ).fetchone()
            if changed is None:
                raise ExecutionRefused("recovery_decision_already_recorded")
        self.event(
            decision.recovery_id,
            {
                "stage": "fresh_approval",
                "decision": decision.decision,
                "review_hash": decision.review_hash,
                "plan_hash": decision.plan_hash,
                "historical_grant_reused": False,
            },
        )

    def state(self, recovery_id, state, result):
        with self.execution.connection() as c:
            c.execute(
                "UPDATE public.agent_recovery_runs SET state=%s,result=%s,result_hash=%s WHERE recovery_id=%s",
                (state, Jsonb(result), document_hash(result), recovery_id),
            )
        self.event(recovery_id, {"stage": state.lower(), "result": result})

    def reserve(self, lease, recovery_id):
        with self.execution.locked(lease.task_id) as (c, row):
            self.execution._live(row, lease)
            runtime = c.execute(
                "SELECT * FROM public.agent_recovery_runtime WHERE task_id=%s FOR UPDATE", (lease.task_id,)
            ).fetchone()
            if runtime["active_recovery_id"] not in (None, recovery_id):
                raise ExecutionRefused("another_recovery_active")
            request = c.execute(
                "SELECT * FROM public.agent_recovery_runs WHERE recovery_id=%s FOR UPDATE", (recovery_id,)
            ).fetchone()
            if request["state"] != "APPROVED":
                raise ExecutionRefused("recovery_not_freshly_approved")
            if request["task_id"] != lease.task_id:
                raise ExecutionRefused("recovery_task_mismatch")
            if datetime.fromisoformat(request["decision"]["expires_at"].replace("Z", "+00:00")) <= datetime.now(
                timezone.utc
            ):
                raise ExecutionRefused("recovery_decision_expired")
            c.execute("UPDATE public.agent_recovery_runs SET state='RESTORING' WHERE recovery_id=%s", (recovery_id,))
            c.execute(
                "UPDATE public.agent_recovery_runtime SET active_recovery_id=%s WHERE task_id=%s",
                (recovery_id, lease.task_id),
            )

    def resolve(self, lease, call_id, resolution):
        with self.execution.locked(lease.task_id) as (c, row):
            self.execution._live(row, lease)
            old = self.execution.call(lease.task_id, call_id)
            if old is None or old["state"] not in {"PREPARED", "RUNNING", "UNKNOWN"}:
                raise ExecutionRefused("recovery_pending_call_changed")
            c.execute(
                "UPDATE public.agent_execution_calls SET state='UNKNOWN',reconciliation=%s,updated_at=clock_timestamp() WHERE task_id=%s AND call_id=%s",
                (Jsonb(resolution), lease.task_id, call_id),
            )
            if row["active_call_id"] == call_id:
                c.execute(
                    "UPDATE public.agent_execution_tasks SET active_call_id=NULL WHERE task_id=%s", (lease.task_id,)
                )

    def rebind(self, lease, recovery_id, worker, workspace, trace, lock_path, task):
        with self.execution.locked(lease.task_id) as (c, row):
            self.execution._live(row, lease)
            runtime = c.execute(
                "SELECT * FROM public.agent_recovery_runtime WHERE task_id=%s FOR UPDATE", (lease.task_id,)
            ).fetchone()
            if runtime["active_recovery_id"] != recovery_id or row["active_call_id"] is not None:
                raise ExecutionRefused("recovery_rebind_not_exclusive")
            payload = task.model_dump(mode="json")
            for key in ["tool_calls_reserved", "model_calls_reserved"]:
                payload[key] = row["payload"][key]
            c.execute(
                "UPDATE public.agent_execution_tasks SET workspace_key=%s,lock_path=%s,worker_identity=%s,payload=%s,blocked_reason=%s WHERE task_id=%s",
                (
                    worker.workspace_key,
                    str(lock_path),
                    Jsonb(worker.model_dump(mode="json")),
                    Jsonb(payload),
                    "recovery_waiting_fresh_activation",
                    lease.task_id,
                ),
            )
            c.execute(
                "UPDATE public.agent_recovery_runtime SET workspace_path=%s,trace_path=%s,lock_path=%s WHERE task_id=%s",
                (str(workspace), str(trace), str(lock_path), lease.task_id),
            )
        return Lease(task_id=lease.task_id, worker_id=lease.worker_id, lease_epoch=lease.lease_epoch, worker=worker)

    def activate(self, lease, recovery_id, control, result):
        with self.execution.locked(lease.task_id) as (c, row):
            self.execution._live(row, lease)
            if (
                not control["authorization_active"]
                or control["authorization"]["authorization_id"] != control["task"]["grant_id"]
            ):
                raise ExecutionRefused("recovery_requires_new_current_authorization")
            if row["active_call_id"] is not None:
                raise ExecutionRefused("recovery_activation_has_pending_call")
            runtime = c.execute(
                "SELECT active_recovery_id FROM public.agent_recovery_runtime WHERE task_id=%s FOR UPDATE",
                (lease.task_id,),
            ).fetchone()
            run = c.execute(
                "SELECT state FROM public.agent_recovery_runs WHERE recovery_id=%s AND task_id=%s FOR UPDATE",
                (recovery_id, lease.task_id),
            ).fetchone()
            if runtime["active_recovery_id"] != recovery_id or run is None or run["state"] != "RESTORING":
                raise ExecutionRefused("recovery_activation_state_changed")
            for key in ("tool_calls_reserved", "model_calls_reserved"):
                if control["task"][key] != row["payload"][key]:
                    raise ExecutionRefused("recovery_counter_reset_refused")
            c.execute(
                "UPDATE public.agent_execution_tasks SET blocked_reason=NULL,payload=%s,authority=%s WHERE task_id=%s",
                (Jsonb(control["task"]), Jsonb({k: v for k, v in control.items() if k != "task"}), lease.task_id),
            )
            c.execute(
                "UPDATE public.agent_recovery_runtime SET active_recovery_id=NULL WHERE task_id=%s AND active_recovery_id=%s",
                (lease.task_id, recovery_id),
            )
            c.execute(
                "UPDATE public.agent_recovery_runs SET state='RESTORED',result=%s,result_hash=%s WHERE recovery_id=%s",
                (Jsonb(result), document_hash(result), recovery_id),
            )
