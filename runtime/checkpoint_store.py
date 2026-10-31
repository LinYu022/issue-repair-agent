"""PostgreSQL checkpoint publication; the intent result and COMMITTED reference share a transaction."""

from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from domain.checkpoints import CheckpointDocument
from domain.execution import ExecutionRefused
from domain.models import ArtifactRef
from domain.plans import document_hash
from runtime.execution_store import SQL_ROOT, PostgresExecutionStore, migrate_execution


def migrate_checkpoints(connection, direction="up"):
    if direction not in {"up", "down", "status"}:
        raise ExecutionRefused("checkpoint_migration_direction")
    checksum = document_hash(
        {name: (SQL_ROOT / f"0003_checkpoints.{name}.sql").read_text(encoding="utf-8") for name in ("up", "down")}
    )
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(2026102811)")
        if migrate_execution(connection, "status") != 1:
            raise ExecutionRefused("execution_schema_required")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS public.agent_checkpoint_schema (version integer PRIMARY KEY, checksum text NOT NULL)"
        )
        rows = connection.execute("SELECT version,checksum FROM public.agent_checkpoint_schema").fetchall()
        if rows and rows != [(1, checksum)]:
            raise ExecutionRefused("checkpoint_migration_checksum")
        if direction == "up" and not rows:
            connection.execute((SQL_ROOT / "0003_checkpoints.up.sql").read_text(encoding="utf-8"))
            connection.execute("INSERT INTO public.agent_checkpoint_schema VALUES (1,%s)", (checksum,))
            return 1
        if direction == "down" and rows:
            if connection.execute("SELECT to_regclass('public.agent_recovery_runtime')").fetchone()[0] is not None:
                raise ExecutionRefused("recovery_schema_must_be_removed_first")
            connection.execute("LOCK TABLE public.agent_checkpoints IN ACCESS EXCLUSIVE MODE")
            if connection.execute("SELECT EXISTS (SELECT 1 FROM public.agent_checkpoints)").fetchone()[0]:
                raise ExecutionRefused("checkpoint_downgrade_data_exists")
            connection.execute((SQL_ROOT / "0003_checkpoints.down.sql").read_text(encoding="utf-8"))
            connection.execute("DELETE FROM public.agent_checkpoint_schema")
            return 0
        return 1 if rows else 0


class PostgresCheckpointStore:
    def __init__(self, execution: PostgresExecutionStore, root: Path):
        self.execution = execution
        self.root = root.resolve()

    def begin(self, lease, checkpoint_id, call_id, boundary):
        with self.execution.locked(lease.task_id) as (c, row):
            self.execution._live(row, lease)
            if row["active_call_id"] != call_id:
                raise ExecutionRefused("checkpoint_intent_not_active")
            c.execute(
                "INSERT INTO public.agent_checkpoints (checkpoint_id,task_id,call_id,lease_epoch,execution_epoch,store_path,state,boundary) VALUES (%s,%s,%s,%s,%s,%s,'PREPARING',%s)",
                (
                    checkpoint_id,
                    lease.task_id,
                    call_id,
                    lease.lease_epoch,
                    row["payload"]["execution_epoch"],
                    str(self.root),
                    Jsonb(boundary),
                ),
            )

    def row(self, checkpoint_id):
        with self.execution.connection() as c:
            c.row_factory = dict_row
            row = c.execute(
                "SELECT * FROM public.agent_checkpoints WHERE checkpoint_id=%s", (checkpoint_id,)
            ).fetchone()
            if row is None or row["store_path"] != str(self.root):
                raise ExecutionRefused("checkpoint_identity_or_store_missing")
            return row

    def committed_rows(self, task_id=None):
        with self.execution.connection() as c:
            c.row_factory = dict_row
            return c.execute(
                "SELECT * FROM public.agent_checkpoints WHERE store_path=%s AND state='COMMITTED' AND (%s::uuid IS NULL OR task_id=%s) ORDER BY created_at,checkpoint_id",
                (str(self.root), task_id, task_id),
            ).fetchall()

    def commit(self, lease, document: CheckpointDocument, reference: ArtifactRef, result, validate):
        # JointCheckpoints holds the graph/workspace/archive locks here. Disk
        # verification must not hold the task row needed by the heartbeat.
        validate()
        with self.execution.locked(lease.task_id) as (c, row):
            self.execution._live(row, lease)
            checkpoint = c.execute(
                "SELECT * FROM public.agent_checkpoints WHERE checkpoint_id=%s FOR UPDATE", (document.checkpoint_id,)
            ).fetchone()
            if checkpoint is None or checkpoint["state"] != "PREPARING" or checkpoint["store_path"] != str(self.root):
                raise ExecutionRefused("checkpoint_not_preparing_here")
            if (document.task_id, document.call_id, document.lease_epoch, document.execution_epoch) != (
                lease.task_id,
                row["active_call_id"],
                lease.lease_epoch,
                row["payload"]["execution_epoch"],
            ):
                raise ExecutionRefused("checkpoint_boundary_owner_changed")
            if document.control_stamp != document_hash({"task": row["payload"], "authority": row["authority"]}):
                raise ExecutionRefused("checkpoint_control_or_authority_changed")
            if document.ledger["deadline"] != str(row["deadline"]):
                raise ExecutionRefused("checkpoint_deadline_changed")
            self.execution._live(self.execution._task(c, lease.task_id), lease)
            active = self.execution.call(lease.task_id, document.call_id)
            if (
                active is None
                or active["state"] != "RUNNING"
                or active["kind"] != "snapshot"
                or active["lease_epoch"] != lease.lease_epoch
            ):
                raise ExecutionRefused("checkpoint_call_not_running")
            c.execute(
                "UPDATE public.agent_checkpoints SET state='COMMITTED',document=%s,document_hash=%s,committed_at=clock_timestamp() WHERE checkpoint_id=%s",
                (Jsonb(reference.model_dump(mode="json")), document_hash(document), document.checkpoint_id),
            )
            c.execute(
                "UPDATE public.agent_execution_calls SET state='SUCCEEDED',result=%s,result_hash=%s,updated_at=clock_timestamp() WHERE task_id=%s AND call_id=%s",
                (Jsonb(result), document_hash(result), lease.task_id, document.call_id),
            )
            c.execute("UPDATE public.agent_execution_tasks SET active_call_id=NULL WHERE task_id=%s", (lease.task_id,))

    def ledger_boundary(self, lease):
        with self.execution.locked(lease.task_id) as (c, row):
            self.execution._live(row, lease)
            calls = c.execute(
                "SELECT * FROM public.agent_execution_calls WHERE task_id=%s ORDER BY created_at,call_id",
                (lease.task_id,),
            ).fetchall()
            # Dates/UUIDs are normalized as audit values, never reactivated on load.
            import json

            normalized = json.loads(json.dumps(calls, default=str))
            return {
                "task": row["payload"],
                "authority": row["authority"],
                "deadline": str(row["deadline"]),
                "calls": normalized,
                "calls_hash": document_hash({"calls": normalized}),
            }
