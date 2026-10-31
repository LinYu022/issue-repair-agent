"""Insert/read immutable task snapshots; not the execution scheduler ledger."""

import os
from collections.abc import Sequence
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from domain.call_records import CallRecord
from domain.tasks import Task
from runtime.migrations import MigrationError, current_version


def database_url() -> str:
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise ValueError("set DATABASE_URL for the dedicated PostgreSQL database")
    return value


class PostgresSnapshotStore:
    def __init__(self, dsn: str):
        if not dsn.strip():
            raise ValueError("DATABASE_URL must be nonempty when configured")
        self._dsn = dsn

    def _connect(self):
        return psycopg.connect(self._dsn, connect_timeout=3, options="-c statement_timeout=3000 -c lock_timeout=1000")

    def check_ready(self) -> None:
        """Read-only schema probe; migrations remain an explicit operator action."""
        with self._connect() as connection:
            if current_version(connection) != 1:
                raise MigrationError("snapshot schema requires migration version 1")
            connection.execute("SELECT task_id, payload FROM public.agent_tasks LIMIT 0")
            connection.execute(
                "SELECT call_kind, call_id, task_id, position, payload FROM public.agent_call_records LIMIT 0"
            )

    def save(self, task: Task, records: Sequence[CallRecord]) -> None:
        if any(record.task_id != task.task_id for record in records):
            raise ValueError("call record belongs to another task")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO public.agent_tasks (task_id, payload) VALUES (%s, %s)",
                (task.task_id, Jsonb(task.model_dump(mode="json"))),
            )
            for position, record in enumerate(records):
                connection.execute(
                    "INSERT INTO public.agent_call_records (call_kind, call_id, task_id, position, payload) VALUES (%s, %s, %s, %s, %s)",
                    (record.call_kind, record.call_id, task.task_id, position, Jsonb(record.model_dump(mode="json"))),
                )

    def read(self, task_id: UUID) -> tuple[Task, list[CallRecord]] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM public.agent_tasks WHERE task_id = %s", (task_id,)).fetchone()
            if row is None:
                return None
            records = connection.execute(
                "SELECT payload FROM public.agent_call_records WHERE task_id = %s ORDER BY position", (task_id,)
            ).fetchall()
            task = Task.model_validate(row[0])
            calls = [CallRecord.model_validate(record[0]) for record in records]
            if task.task_id != task_id or any(record.task_id != task_id for record in calls):
                raise ValueError("stored task and call identities do not match")
            return task, calls
