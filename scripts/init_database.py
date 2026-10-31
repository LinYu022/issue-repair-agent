"""Initialize the application schemas in the operator-selected PostgreSQL database."""

import psycopg

from runtime.checkpoint_store import migrate_checkpoints
from runtime.episode_store import migrate_episodic_memory
from runtime.execution_store import migrate_execution
from runtime.migrations import migrate
from runtime.postgres import database_url
from runtime.recovery_store import migrate_recovery
from runtime.semantic_store import migrate_semantic_memory
from runtime.skill_store import migrate_procedural_memory


def main():
    with psycopg.connect(database_url(), autocommit=True) as connection:
        for action in (
            migrate,
            migrate_execution,
            migrate_checkpoints,
            migrate_recovery,
            migrate_semantic_memory,
            migrate_episodic_memory,
            migrate_procedural_memory,
        ):
            action(connection, "up")
    print("Database initialized")


if __name__ == "__main__":
    main()
