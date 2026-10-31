"""Immutable experiment history bundles, restored only into empty disposable databases."""

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg import sql
from psycopg.types.json import Jsonb

from domain.memory_experiments import VisibilityManifest
from domain.plans import document_hash
from runtime.semantic_store import SemanticStore

TABLES = (
    "agent_execution_tasks",
    "agent_execution_calls",
    "agent_memory_namespaces",
    "agent_memory_sources",
    "agent_memory_jobs",
    "agent_semantic_memories",
    "agent_memory_revisions",
    "agent_memory_dependencies",
    "agent_memory_active_index",
    "agent_memory_cache",
    "agent_memory_events",
    "agent_episodes",
    "agent_episode_blobs",
    "agent_episode_index",
    "agent_episode_dependencies",
    "agent_episode_parents",
    "agent_episode_usage",
    "agent_skills",
    "agent_skill_revisions",
    "agent_skill_sources",
    "agent_skill_validation_tasks",
    "agent_skill_attempts",
    "agent_skill_events",
)


def normalized(value):
    return json.loads(json.dumps(value, default=str))


def capture(execution, user_id, facts):
    with SemanticStore(execution).transaction(user_id) as (connection, _):
        users = connection.execute("SELECT user_id FROM public.agent_memory_namespaces").fetchall()
        if len(users) != 1 or users[0]["user_id"] != user_id:
            raise ValueError("memory_snapshot_requires_isolated_single_user_database")
        tasks = connection.execute(
            "SELECT task_id,quiescent,active_call_id FROM public.agent_execution_tasks"
        ).fetchall()
        if any(not t["quiescent"] or t["active_call_id"] is not None for t in tasks):
            raise ValueError("memory_snapshot_requires_closed_quiescent_sources")
        rows = {}
        for table in TABLES:
            rows[table] = normalized(
                connection.execute(sql.SQL("SELECT * FROM public.{}").format(sql.Identifier(table))).fetchall()
            )
            rows[table].sort(key=document_hash)
        body = {
            "schema": 1,
            "user_id": str(user_id),
            "tables": rows,
            "facts": [f.model_dump(mode="json") for f in facts],
        }
        return {"body": body, "sha256": document_hash(body)}


def verify(snapshot):
    if set(snapshot) != {"body", "sha256"} or document_hash(snapshot["body"]) != snapshot["sha256"]:
        raise ValueError("memory_snapshot_integrity")
    body = snapshot["body"]
    if (
        set(body) != {"schema", "user_id", "tables", "facts"}
        or body["schema"] != 1
        or set(body["tables"]) != set(TABLES)
    ):
        raise ValueError("memory_snapshot_inventory")
    UUID(body["user_id"])
    if len(json.dumps(body).encode()) > 32 * 1024 * 1024:
        raise ValueError("memory_snapshot_bounded_size")
    from domain.memory_experiments import VisibleFact

    facts = {value["fact_id"]: VisibleFact.model_validate(value) for value in body["facts"]}
    if len(facts) != len(body["facts"]):
        raise ValueError("snapshot_unique_fact_identity")
    tables = body["tables"]
    bindings = {}
    for row in tables["agent_execution_tasks"]:
        bindings["task:" + row["task_id"]] = document_hash(row["payload"])
    for row in tables["agent_memory_sources"]:
        bindings["source:" + row["source_id"]] = row["payload_hash"]
    for row in tables["agent_memory_revisions"]:
        bindings[f"semantic:{row['memory_id']}:{row['revision']}"] = row["document_hash"]
    for row in tables["agent_episodes"]:
        bindings["episode:" + row["episode_id"]] = row["document_hash"]
    for row in tables["agent_skill_events"]:
        if row["payload"]["kind"] == "source_independently_checked":
            bindings["source-check:" + row["payload_hash"]] = row["payload_hash"]
    for row in tables["agent_skill_attempts"]:
        if row["state"] == "PASSED" and row["mode"] == "validation":
            bindings["attempt:" + row["attempt_id"]] = row["evidence_hash"]
    for row in tables["agent_skill_revisions"]:
        key = f"skill:{row['skill_id']}:{row['revision']}"
        bindings[key] = row["document_hash"]
        if key in facts:
            required = {"episode:" + ref["episode_id"] for ref in row["document"]["sources"]}
            required.update(
                "attempt:" + a["attempt_id"]
                for a in tables["agent_skill_attempts"]
                if (a["skill_id"], a["revision"], a["state"], a["mode"])
                == (row["skill_id"], row["revision"], "PASSED", "validation")
            )
            if not any(item.startswith("attempt:") for item in required) or not required <= set(
                facts[key].dependencies
            ):
                raise ValueError("snapshot_skill_cannot_hide_later_validation")
    if any(key not in bindings or facts[key].document_hash != bindings[key] for key in facts):
        raise ValueError("snapshot_fact_to_actual_record_binding")
    return body


def write(path: Path, snapshot):
    verify(snapshot)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")


def read(path: Path):
    value = json.loads(path.read_text(encoding="utf-8"))
    verify(value)
    return value


def visible_prefix(snapshot, manifest: VisibilityManifest):
    body = verify(snapshot)
    if body["facts"] != [f.model_dump(mode="json") for f in manifest.facts]:
        raise ValueError("snapshot_visibility_manifest_mismatch")
    allowed = set(manifest.allowed)
    tables = body["tables"]
    memories = {
        r["memory_id"]
        for r in tables["agent_semantic_memories"]
        if f"semantic:{r['memory_id']}:{r['current_revision']}" in allowed
    }
    episodes = {r["episode_id"] for r in tables["agent_episodes"] if f"episode:{r['episode_id']}" in allowed}
    skills = {
        (r["skill_id"], r["revision"])
        for r in tables["agent_skill_revisions"]
        if f"skill:{r['skill_id']}:{r['revision']}" in allowed
    }
    sources = {r["source_id"] for r in tables["agent_memory_sources"] if "source:" + r["source_id"] in allowed}
    selected: dict[str, list[dict[str, Any]]] = {name: [] for name in TABLES}
    for name, rows in tables.items():
        for row in rows:
            match name:
                case "agent_execution_tasks" | "agent_execution_calls":
                    keep = "task:" + row["task_id"] in allowed
                case "agent_memory_namespaces":
                    keep = True
                case "agent_memory_sources" | "agent_memory_jobs":
                    keep = row["source_id"] in sources
                case "agent_semantic_memories" | "agent_memory_revisions" | "agent_memory_active_index":
                    keep = row["memory_id"] in memories
                case "agent_memory_dependencies":
                    keep = row["parent_id"] in memories and row["child_id"] in memories
                case "agent_memory_cache" | "agent_memory_events" | "agent_episode_usage":
                    keep = False  # Consumer cache/events are not facts and must never select the prefix.
                case "agent_episodes" | "agent_episode_blobs" | "agent_episode_index":
                    keep = row["episode_id"] in episodes
                case "agent_episode_dependencies":
                    keep = row["episode_id"] in episodes and row["parent_id"] in memories
                case "agent_episode_parents":
                    keep = row["child_id"] in episodes and row["parent_id"] in episodes
                case "agent_skills":
                    keep = any(key[0] == row["skill_id"] for key in skills)
                    row = {
                        **row,
                        "enabled_revision": row["enabled_revision"]
                        if (row["skill_id"], row["enabled_revision"]) in skills
                        else None,
                        "latest_revision": max((key[1] for key in skills if key[0] == row["skill_id"]), default=1),
                    }
                case "agent_skill_revisions" | "agent_skill_sources":
                    keep = (row["skill_id"], row["revision"]) in skills
                case "agent_skill_validation_tasks":
                    keep = "task:" + row["task_id"] in allowed
                case "agent_skill_attempts":
                    keep = (row["skill_id"], row["revision"]) in skills and "attempt:" + row["attempt_id"] in allowed
                case "agent_skill_events":
                    # Keep only separately disclosed source-check receipts. All
                    # promotion proofs are in the validated attempt rows above.
                    payload = row["payload"]
                    keep = (
                        payload["kind"] == "source_independently_checked"
                        and "source-check:" + row["payload_hash"] in allowed
                    )
            if keep:
                selected[name].append(row)
    # Retained event audit must have its own contiguous namespace sequence.
    for i, row in enumerate(selected["agent_skill_events"], 1):
        row = {**row, "sequence": i}
        selected["agent_skill_events"][i - 1] = row
    for rows in selected.values():
        rows.sort(key=document_hash)
    result = {
        "schema": 1,
        "user_id": body["user_id"],
        "tables": selected,
        "facts": [f.model_dump(mode="json") for f in manifest.facts if f.fact_id in allowed],
    }
    return {"body": result, "sha256": document_hash(result)}


def restore(execution, snapshot):
    body = verify(snapshot)
    with execution.connection() as connection:
        for table in TABLES:
            if connection.execute(
                sql.SQL("SELECT EXISTS(SELECT 1 FROM public.{})").format(sql.Identifier(table))
            ).fetchone()[0]:
                raise ValueError("snapshot_restore_requires_empty_disposable_database")
        name = connection.execute("SELECT current_database()").fetchone()[0]
        if not name.startswith("issue_agent_test_"):
            raise ValueError("snapshot_restore_only_disposable_database")
        for table in TABLES:
            for row in body["tables"][table]:
                fields = list(row)
                connection.execute(
                    sql.SQL("INSERT INTO public.{} ({}) VALUES ({})").format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, fields)),
                        sql.SQL(",").join(sql.Placeholder() for _ in fields),
                    ),
                    [Jsonb(row[key]) if isinstance(row[key], (list, dict)) else row[key] for key in fields],
                )
    return snapshot["sha256"]
