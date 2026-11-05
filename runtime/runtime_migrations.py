"""Install the Runtime v2 append-only schema independently of the legacy schema."""

from __future__ import annotations

import hashlib
from pathlib import Path

from psycopg import Connection

ROOT = Path(__file__).resolve().parents[1] / "migrations"
VERSIONS = tuple(range(10, 16))


def _checksum(version: int) -> str:
    path = next(ROOT.glob(f"{version:04d}_*.up.sql"))
    content = path.read_bytes()
    return hashlib.sha256(content).hexdigest()


def migrate_runtime(connection: Connection, direction: str = "up") -> int:
    if direction not in {"up", "down"}:
        raise ValueError("migration direction must be up or down")
    with connection.transaction():
        connection.execute("CREATE TABLE IF NOT EXISTS public.agent_runtime_schema_migrations (version integer PRIMARY KEY, checksum text NOT NULL)")
        rows = connection.execute("SELECT version, checksum FROM public.agent_runtime_schema_migrations ORDER BY version").fetchall()
        current = rows[-1][0] if rows else 9
        if direction == "up":
            for version in VERSIONS:
                if version <= current:
                    continue
                path = next(ROOT.glob(f"{version:04d}_*.up.sql"))
                connection.execute(path.read_text(encoding="utf-8"))
                connection.execute("INSERT INTO public.agent_runtime_schema_migrations(version,checksum) VALUES(%s,%s)", (version, _checksum(version)))
                current = version
            return current
        if current >= 10:
            path = next(ROOT.glob(f"{current:04d}_*.down.sql"))
            connection.execute(path.read_text(encoding="utf-8"))
            connection.execute("DELETE FROM public.agent_runtime_schema_migrations WHERE version=%s", (current,))
            return current - 1
        return current
