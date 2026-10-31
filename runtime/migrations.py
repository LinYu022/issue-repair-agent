"""Explicit first PostgreSQL migration with a checksum and transactional DDL."""

import hashlib
from pathlib import Path
from typing import Literal

from psycopg import Connection

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
UP = MIGRATIONS / "0001_initial.up.sql"
DOWN = MIGRATIONS / "0001_initial.down.sql"


class MigrationError(ValueError):
    """A safe, actionable migration refusal without connection details."""


def migration_checksum() -> str:
    content = UP.read_text(encoding="utf-8") + "\0" + DOWN.read_text(encoding="utf-8")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def current_version(connection: Connection) -> int:
    row = connection.execute("SELECT to_regclass('public.agent_schema_migrations')").fetchone()
    if row is None:
        raise MigrationError("migration probe returned no row")
    if row[0] is None:
        return 0
    rows = connection.execute(
        "SELECT version, checksum FROM public.agent_schema_migrations ORDER BY version"
    ).fetchall()
    if not rows:
        return 0
    if rows != [(1, migration_checksum())]:
        raise MigrationError("unknown migration version or changed migration checksum")
    return 1


def migrate(connection: Connection, direction: Literal["up", "down"]) -> int:
    if direction not in {"up", "down"}:
        raise MigrationError("migration direction must be up or down")
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout = '3s'")
        connection.execute("SET LOCAL statement_timeout = '10s'")
        connection.execute("SELECT pg_advisory_xact_lock(2026102601)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS public.agent_schema_migrations (version integer PRIMARY KEY, checksum text NOT NULL)"
        )
        version = current_version(connection)
        if direction == "up" and version == 0:
            connection.execute(UP.read_text(encoding="utf-8"))
            connection.execute(
                "INSERT INTO public.agent_schema_migrations (version, checksum) VALUES (1, %s)", (migration_checksum(),)
            )
            return 1
        if direction == "down" and version == 1:
            companion = connection.execute("SELECT to_regclass('public.agent_execution_tasks')").fetchone()
            if companion is None or companion[0] is not None:
                raise MigrationError("refuse downgrade: execution companion schema must be removed first")
            connection.execute("LOCK TABLE public.agent_tasks, public.agent_call_records IN ACCESS EXCLUSIVE MODE")
            row = connection.execute(
                "SELECT EXISTS (SELECT 1 FROM public.agent_tasks) OR EXISTS (SELECT 1 FROM public.agent_call_records)"
            ).fetchone()
            if row is None:
                raise MigrationError("data protection probe returned no row")
            if row[0]:
                raise MigrationError("refuse downgrade: task or call data exists")
            connection.execute(DOWN.read_text(encoding="utf-8"))
            connection.execute("DELETE FROM public.agent_schema_migrations WHERE version = 1")
            return 0
        return version
