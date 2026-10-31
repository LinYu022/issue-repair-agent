"""PostgreSQL row-locked scheduler and intent ledger. Physical exclusion is mandatory too."""

from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from domain.execution import CallIntent, ExecutionRefused, Lease, WorkerIdentity
from domain.plans import document_hash
from domain.tasks import Task
from runtime.migrations import current_version

SQL_ROOT = Path(__file__).resolve().parents[1] / "migrations"


def migrate_execution(connection, direction="up"):
    """Explicit companion migration; the immutable snapshot schema remains version 1."""
    if direction not in {"up", "down", "status"}:
        raise ExecutionRefused("execution_migration_direction")
    checksum = document_hash(
        {name: (SQL_ROOT / f"0002_execution.{name}.sql").read_text(encoding="utf-8") for name in ("up", "down")}
    )
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(2026102810)")
        if current_version(connection) != 1:
            raise ExecutionRefused("snapshot_schema_required")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS public.agent_execution_schema (version integer PRIMARY KEY, checksum text NOT NULL)"
        )
        rows = connection.execute("SELECT version, checksum FROM public.agent_execution_schema").fetchall()
        if rows and rows != [(1, checksum)]:
            raise ExecutionRefused("execution_migration_checksum")
        if direction == "up" and not rows:
            connection.execute((SQL_ROOT / "0002_execution.up.sql").read_text(encoding="utf-8"))
            connection.execute("INSERT INTO public.agent_execution_schema VALUES (1,%s)", (checksum,))
            return 1
        if direction == "down" and rows:
            if connection.execute("SELECT to_regclass('public.agent_memory_namespaces')").fetchone()[0] is not None:
                raise ExecutionRefused("semantic_memory_schema_must_be_removed_first")
            if connection.execute("SELECT to_regclass('public.agent_checkpoints')").fetchone()[0] is not None:
                raise ExecutionRefused("checkpoint_schema_must_be_removed_first")
            connection.execute("LOCK TABLE public.agent_execution_tasks IN ACCESS EXCLUSIVE MODE")
            if connection.execute("SELECT EXISTS (SELECT 1 FROM public.agent_execution_tasks)").fetchone()[0]:
                raise ExecutionRefused("execution_downgrade_data_exists")
            connection.execute((SQL_ROOT / "0002_execution.down.sql").read_text(encoding="utf-8"))
            connection.execute("DELETE FROM public.agent_execution_schema")
            return 0
        return 1 if rows else 0


class PostgresExecutionStore:
    def __init__(self, dsn):
        self._dsn = dsn

    @contextmanager
    def connection(self):
        try:
            with psycopg.connect(
                self._dsn, connect_timeout=3, options="-c statement_timeout=3000 -c lock_timeout=1000"
            ) as connection:
                yield connection
        except psycopg.Error:
            raise ExecutionRefused("execution_database_unavailable") from None

    def register(self, task: Task, workspace_key: str, authority: dict, lock_path=""):
        with self.connection() as c:
            c.execute(
                "INSERT INTO public.agent_execution_tasks (task_id,workspace_key,lock_path,payload,authority,fixed_limits,deadline) "
                "VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp() + %s * interval '1 second')",
                (
                    task.task_id,
                    workspace_key,
                    lock_path,
                    Jsonb(task.model_dump(mode="json")),
                    Jsonb(authority),
                    Jsonb(task.limits.model_dump()),
                    task.limits.max_wall_seconds,
                ),
            )

    @staticmethod
    def _task(c, task_id):
        row = c.execute(
            "SELECT *, lease_until > clock_timestamp() AS lease_live, deadline > clock_timestamp() AS within_deadline "
            "FROM public.agent_execution_tasks WHERE task_id=%s FOR UPDATE",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ExecutionRefused("execution_task_missing")
        return row

    @staticmethod
    def _live(row, lease: Lease, *, cancellation=False):
        if (row["worker_id"], row["lease_epoch"]) != (lease.worker_id, lease.lease_epoch):
            raise ExecutionRefused("stale_worker_lease")
        if row["worker_identity"] != lease.worker.model_dump(mode="json") or not row["lease_live"]:
            raise ExecutionRefused("worker_identity_or_lease_expired")
        if row["cancelled"] and not cancellation:
            raise ExecutionRefused("persistent_task_cancelled")
        if not row["within_deadline"] and not cancellation:
            raise ExecutionRefused("persistent_task_deadline")

    @contextmanager
    def locked(self, task_id):
        with self.connection() as c:
            c.row_factory = dict_row
            yield c, self._task(c, task_id)

    def read(self, task_id):
        with self.locked(task_id) as (_, row):
            return row

    def claim(self, task_id: UUID, worker_id: UUID, worker: WorkerIdentity, *, seconds=15):
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise ExecutionRefused("bounded_lease_seconds_required")
        with self.locked(task_id) as (c, row):
            if row["cancelled"] or not row["within_deadline"]:
                raise ExecutionRefused("task_not_claimable")
            if row["payload"]["lifecycle"] in {"COMPLETED", "FAILED", "CANCELLED"}:
                raise ExecutionRefused("task_terminal")
            if row["workspace_key"] != worker.workspace_key:
                raise ExecutionRefused("worker_workspace_mismatch")
            if row["lease_live"]:
                raise ExecutionRefused("lease_still_owned")
            # Caller must hold the lifetime OS lock and verify Docker quiescence.
            # DB-only callers are trusted controller components, never model actions.
            epoch = row["lease_epoch"] + 1
            c.execute(
                "UPDATE public.agent_execution_tasks SET worker_id=%s,worker_identity=%s,lease_epoch=%s,"
                "lease_until=clock_timestamp()+%s*interval '1 second',quiescent=false WHERE task_id=%s",
                (worker_id, Jsonb(worker.model_dump(mode="json")), epoch, seconds, task_id),
            )
            if row["worker_id"] is not None:
                payload = {**row["payload"], "lifecycle": "PAUSED", "grant_id": None, "revision_id": None}
                c.execute(
                    "UPDATE public.agent_execution_tasks SET payload=%s,authority=%s,blocked_reason=%s WHERE task_id=%s",
                    (
                        Jsonb(payload),
                        Jsonb({"restored_authority": False}),
                        "takeover_requires_reconciliation_and_fresh_authority",
                        task_id,
                    ),
                )
            return Lease(task_id=task_id, worker_id=worker_id, lease_epoch=epoch, worker=worker)

    def live(self, lease):
        with self.locked(lease.task_id) as (_, row):
            self._live(row, lease)
            return row

    def heartbeat(self, lease, *, seconds=15):
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise ExecutionRefused("bounded_lease_seconds_required")
        with self.locked(lease.task_id) as (c, row):
            self._live(row, lease)
            c.execute(
                "UPDATE public.agent_execution_tasks SET lease_until=clock_timestamp()+%s*interval '1 second' WHERE task_id=%s",
                (seconds, lease.task_id),
            )

    def sync(self, lease, task: Task, authority: dict):
        with self.locked(lease.task_id) as (c, row):
            self._live(row, lease, cancellation=True)
            if task.task_id != lease.task_id or any(
                value > row["fixed_limits"][key] for key, value in task.limits.model_dump().items()
            ):
                raise ExecutionRefused("persistent_task_identity_or_limits_changed")
            payload = task.model_dump(mode="json")
            if row["active_call_id"] is not None and payload["lifecycle"] in {"COMPLETED", "FAILED"}:
                active = self.call(lease.task_id, row["active_call_id"])
                if active and active["kind"] == "export":
                    payload["lifecycle"] = row["payload"]["lifecycle"]
            for key in ("tool_calls_reserved", "model_calls_reserved"):
                payload[key] = row["payload"][key]
            if row["cancelled"] or task.lifecycle.value == "CANCELLED":
                payload["lifecycle"] = "CANCELLED"
            c.execute(
                "UPDATE public.agent_execution_tasks SET payload=%s,authority=%s,cancelled=%s WHERE task_id=%s",
                (Jsonb(payload), Jsonb(authority), payload["lifecycle"] == "CANCELLED", lease.task_id),
            )
            return Task.model_validate(payload)

    def cancel(self, task_id):
        with self.locked(task_id) as (c, row):
            if row["payload"]["lifecycle"] in {"COMPLETED", "FAILED"}:
                return
            payload = {**row["payload"], "lifecycle": "CANCELLED"}
            c.execute(
                "UPDATE public.agent_execution_tasks SET cancelled=true,payload=%s WHERE task_id=%s",
                (Jsonb(payload), task_id),
            )

    def call(self, task_id, call_id):
        with self.connection() as c:
            c.row_factory = dict_row
            row = c.execute(
                "SELECT * FROM public.agent_execution_calls WHERE task_id=%s AND call_id=%s", (task_id, call_id)
            ).fetchone()
            if row is not None:
                if document_hash(row["intent"]) != row["intent_hash"]:
                    raise ExecutionRefused("intent_integrity_mismatch")
                try:
                    intent = CallIntent.model_validate(row["intent"])
                except ValueError:
                    raise ExecutionRefused("intent_schema_integrity_mismatch") from None
                if (intent.task_id, intent.call_id, intent.kind, intent.operation, intent.execution_epoch) != (
                    row["task_id"],
                    row["call_id"],
                    row["kind"],
                    row["operation"],
                    row["execution_epoch"],
                ):
                    raise ExecutionRefused("intent_index_identity_mismatch")
                if row["result"] is not None and document_hash(row["result"]) != row["result_hash"]:
                    raise ExecutionRefused("result_integrity_mismatch")
            return row

    def prepare(self, lease, intent: CallIntent):
        with self.locked(lease.task_id) as (c, row):
            self._live(row, lease)
            if intent.task_id != lease.task_id:
                raise ExecutionRefused("intent_task_mismatch")
            existing = c.execute(
                "SELECT * FROM public.agent_execution_calls WHERE task_id=%s AND call_id=%s",
                (lease.task_id, intent.call_id),
            ).fetchone()
            if existing:
                if existing["intent_hash"] != document_hash(intent):
                    raise ExecutionRefused("idempotency_key_payload_conflict")
                if existing["state"] in {"SUCCEEDED", "FAILED"}:
                    return self.call(lease.task_id, intent.call_id)
                raise ExecutionRefused("existing_call_requires_reconciliation")
            if row["active_call_id"] is not None or row["blocked_reason"]:
                raise ExecutionRefused("writer_has_pending_or_unknown_call")
            if intent.execution_epoch != row["payload"]["execution_epoch"]:
                raise ExecutionRefused("intent_control_epoch_changed")
            if intent.retry_of:
                previous = self.call(lease.task_id, intent.retry_of)
                if previous is None or (previous["kind"], previous["operation"]) != (intent.kind, intent.operation):
                    raise ExecutionRefused("retry_origin_mismatch")
                safe_unknown = previous["reconciliation"] and previous["reconciliation"]["outcome"] == "not_applied"
                if previous["state"] != "FAILED" and not safe_unknown:
                    raise ExecutionRefused("unknown_result_cannot_be_replayed")
            payload = dict(row["payload"])
            budget = "model" if intent.kind == "model" else "tool" if intent.kind == "tool" else None
            if budget:
                counter = budget + "_calls_reserved"
                if payload[counter] >= payload["limits"]["max_" + budget + "_calls"]:
                    raise ExecutionRefused("persistent_call_budget_exhausted")
                payload[counter] += 1
            c.execute(
                "INSERT INTO public.agent_execution_calls (task_id,call_id,lease_epoch,worker_id,execution_epoch,kind,operation,state,intent,intent_hash,billing_state) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'PREPARED',%s,%s,%s)",
                (
                    lease.task_id,
                    intent.call_id,
                    lease.lease_epoch,
                    lease.worker_id,
                    intent.execution_epoch,
                    intent.kind,
                    intent.operation,
                    Jsonb(intent.model_dump(mode="json")),
                    document_hash(intent),
                    "confirmed_no_dispatch" if intent.kind == "model" else "not_applicable",
                ),
            )
            c.execute(
                "UPDATE public.agent_execution_tasks SET active_call_id=%s,payload=%s WHERE task_id=%s",
                (intent.call_id, Jsonb(payload), lease.task_id),
            )
        return None

    def running(self, lease, call_id):
        with self.locked(lease.task_id) as (c, row):
            self._live(row, lease)
            if row["active_call_id"] != call_id:
                raise ExecutionRefused("call_not_active")
            changed = c.execute(
                "UPDATE public.agent_execution_calls SET state='RUNNING',updated_at=clock_timestamp(),"
                "billing_state=CASE WHEN kind='model' THEN 'possible' ELSE billing_state END "
                "WHERE task_id=%s AND call_id=%s AND state='PREPARED' AND lease_epoch=%s RETURNING call_id",
                (lease.task_id, call_id, lease.lease_epoch),
            ).fetchone()
            if changed is None:
                raise ExecutionRefused("call_not_prepared")

    def process(self, lease, call_id, identity):
        with self.locked(lease.task_id) as (c, row):
            self._live(row, lease, cancellation=True)
            if row["active_call_id"] != call_id:
                raise ExecutionRefused("process_call_not_active")
            c.execute(
                "UPDATE public.agent_execution_calls SET process=%s WHERE task_id=%s AND call_id=%s",
                (Jsonb(identity), lease.task_id, call_id),
            )

    def finish(self, lease, call_id, status, result):
        if status not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise ExecutionRefused("terminal_call_state_required")
        with self.locked(lease.task_id) as (c, row):
            self._live(row, lease, cancellation=True)
            existing = self.call(lease.task_id, call_id)
            if existing is None or existing["lease_epoch"] != lease.lease_epoch or row["active_call_id"] != call_id:
                raise ExecutionRefused("late_or_duplicate_call_result")
            if existing["state"] not in {"PREPARED", "RUNNING"}:
                raise ExecutionRefused("call_result_already_committed")
            c.execute(
                "UPDATE public.agent_execution_calls SET state=%s,result=%s,result_hash=%s,updated_at=clock_timestamp() "
                "WHERE task_id=%s AND call_id=%s",
                (status, Jsonb(result), document_hash(result), lease.task_id, call_id),
            )
            c.execute(
                "UPDATE public.agent_execution_tasks SET active_call_id=%s,blocked_reason=%s WHERE task_id=%s",
                (
                    call_id if status == "UNKNOWN" else None,
                    "unknown_call_reconciliation_required" if status == "UNKNOWN" else None,
                    lease.task_id,
                ),
            )
            if existing["kind"] == "export" and status == "SUCCEEDED" and "task" in result and not row["cancelled"]:
                payload = Task.model_validate(result["task"])
                if payload.task_id != lease.task_id:
                    raise ExecutionRefused("export_task_identity_conflict")
                c.execute(
                    "UPDATE public.agent_execution_tasks SET payload=%s WHERE task_id=%s",
                    (Jsonb(payload.model_dump(mode="json")), lease.task_id),
                )

    def pending(self):
        with self.connection() as c:
            return [
                r[0]
                for r in c.execute(
                    "SELECT task_id FROM public.agent_execution_tasks WHERE NOT cancelled AND deadline>clock_timestamp() AND (lease_until IS NULL OR lease_until<=clock_timestamp()) AND payload->>'lifecycle' NOT IN ('COMPLETED','FAILED','CANCELLED') ORDER BY created_at"
                ).fetchall()
            ]

    def reconcile(self, lease, call_id, evidence: dict):
        """Trusted physical inspection only; no automatic dispatch or old-authority restore."""
        if evidence.get("outcome") not in {
            "applied",
            "not_applied",
            "recorded_result",
            "unknown_model_billing",
            "conflict",
        }:
            raise ExecutionRefused("reconciliation_evidence_required")
        with self.locked(lease.task_id) as (c, row):
            self._live(row, lease, cancellation=True)
            call = self.call(lease.task_id, call_id)
            if (
                call is None
                or call["state"] not in {"PREPARED", "RUNNING", "UNKNOWN"}
                or row["active_call_id"] != call_id
                or call["reconciliation"] is not None
            ):
                raise ExecutionRefused("reconciliation_call_not_pending")
            c.execute(
                "UPDATE public.agent_execution_calls SET state='UNKNOWN',reconciliation=%s,updated_at=clock_timestamp() WHERE task_id=%s AND call_id=%s",
                (Jsonb(evidence), lease.task_id, call_id),
            )
            conflict = evidence["outcome"] == "conflict"
            payload = {
                **row["payload"],
                "lifecycle": "CANCELLED" if row["cancelled"] else "PAUSED",
                "grant_id": None,
                "revision_id": None,
                "self_test_status": "NOT_RUN",
            }
            if not conflict and "actual_workspace_version" in evidence:
                payload["workspace_version"] = evidence["actual_workspace_version"]
            c.execute(
                "UPDATE public.agent_execution_tasks SET payload=%s,authority=%s WHERE task_id=%s",
                (Jsonb(payload), Jsonb({"restored_authority": False}), lease.task_id),
            )
            c.execute(
                "UPDATE public.agent_execution_tasks SET active_call_id=%s,blocked_reason=%s WHERE task_id=%s",
                (
                    call_id if conflict else None,
                    "reconciliation_conflict" if conflict else "fresh_authority_and_session_required",
                    lease.task_id,
                ),
            )

    def release(self, lease):
        with self.locked(lease.task_id) as (c, row):
            self._live(row, lease, cancellation=True)
            c.execute(
                "UPDATE public.agent_execution_tasks SET lease_until=NULL,quiescent=true WHERE task_id=%s",
                (lease.task_id,),
            )
