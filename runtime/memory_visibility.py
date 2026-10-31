"""Build dependency-complete protocol facts from actual public memory records, never evaluator outcomes."""

from datetime import datetime, timezone

from domain.memory_experiments import VisibleFact
from domain.plans import document_hash
from runtime.memory_snapshot import verify


def catalog(snapshot, schedule, *, operator_step=0):
    tables = verify(snapshot)["tables"]
    tasks = {r["task_id"]: r for r in tables["agent_execution_tasks"]}
    sources = {r["source_id"]: r for r in tables["agent_memory_sources"]}
    facts = {}
    now = datetime.now(timezone.utc)

    def add(key, kind, digest, instance, repo, step, deps=(), origin="visible_task", historical=None, executed=None):
        facts[key] = VisibleFact(
            fact_id=key,
            kind=kind,
            document_hash=digest,
            source_instance=instance,
            repo=repo,
            available_step=step,
            historical_at=historical,
            executed_at=executed or now,
            dependencies=tuple(dict.fromkeys(deps)),
            origin=origin,
        )

    for key, row in tasks.items():
        task = row["payload"]
        instance = task["source_task_id"]
        if instance not in schedule:
            raise ValueError("all_memory_source_tasks_require_declared_protocol_availability")
        add(
            "task:" + key,
            "public_source",
            document_hash(task),
            instance,
            task["repo"],
            schedule[instance],
            historical=task["available_at"],
            executed=row["created_at"],
        )
    for key, row in sources.items():
        source = row["payload"]
        evidence = source["evidence"]
        tid = evidence.get("task_id")
        dependencies = ["task:" + tid] if tid else []
        dependencies.extend(f"semantic:{d['memory_id']}:{d['revision']}" for d in source["dependencies"])
        instance = tasks[tid]["payload"]["source_task_id"] if tid else "declared-operator-source:" + key
        step = schedule[instance] if tid else operator_step
        add(
            "source:" + key,
            "public_source",
            row["payload_hash"],
            instance,
            row["repo"],
            step,
            dependencies,
            origin="operator_confirmation" if not tid else "visible_task",
            executed=row["available_at"],
        )
    for row in tables["agent_memory_revisions"]:
        doc = row["document"]
        key = f"semantic:{row['memory_id']}:{row['revision']}"
        source = facts["source:" + doc["source_id"]]
        add(
            key,
            "semantic",
            row["document_hash"],
            source.source_instance,
            doc["scope"]["repo"],
            source.available_step,
            (source.fact_id, *(f"semantic:{d['memory_id']}:{d['revision']}" for d in doc["dependencies"])),
        )
    for row in tables["agent_episodes"]:
        doc = row["document"]
        deps = ["task:" + doc["source_task_id"]]
        deps += ["episode:" + p["episode_id"] for p in doc["parent_episodes"]]
        deps += [f"semantic:{p['memory_id']}:{p['revision']}" for p in doc["semantic_dependencies"]]
        add(
            "episode:" + row["episode_id"],
            "episode",
            row["document_hash"],
            doc["source_instance_id"],
            row["repo"],
            schedule[doc["source_instance_id"]],
            deps,
            executed=row["available_at"],
        )
    for row in tables["agent_skill_events"]:
        value = row["payload"]
        if value["kind"] != "source_independently_checked":
            continue
        detail = value["details"]
        if detail.get("hidden_labels") is not False or not detail.get("disclosed_suite_hash"):
            raise ValueError("undisclosed_or_hidden_source_validation_cannot_enter_experiment_history")
        parent = facts["episode:" + detail["episode_id"]]
        add(
            "source-check:" + row["payload_hash"],
            "public_validation",
            row["payload_hash"],
            parent.source_instance,
            parent.repo,
            parent.available_step,
            (parent.fact_id,),
            origin="visible_development_validation",
            executed=row["created_at"],
        )
    for row in tables["agent_skill_attempts"]:
        if row["state"] != "PASSED" or row["mode"] != "validation":
            continue
        evidence = row["evidence"]
        if evidence.get("hidden_labels") is not False or not evidence.get("disclosed_suite_hash"):
            raise ValueError("undisclosed_or_hidden_transfer_validation_refused")
        parent = facts["episode:" + evidence["episode_id"]]
        add(
            "attempt:" + row["attempt_id"],
            "public_validation",
            row["evidence_hash"],
            parent.source_instance,
            parent.repo,
            parent.available_step,
            (parent.fact_id,),
            origin="visible_development_validation",
            executed=row["completed_at"],
        )
    for row in tables["agent_skill_revisions"]:
        doc = row["document"]
        deps = ["episode:" + ref["episode_id"] for ref in doc["sources"]]
        source_ids = {s["episode_id"] for s in doc["sources"]}
        deps += [
            "source-check:" + e["payload_hash"]
            for e in tables["agent_skill_events"]
            if e["payload"]["kind"] == "source_independently_checked"
            and e["payload"]["details"]["episode_id"] in source_ids
        ]
        deps += [
            "attempt:" + a["attempt_id"]
            for a in tables["agent_skill_attempts"]
            if a["skill_id"] == row["skill_id"]
            and a["revision"] == row["revision"]
            and a["mode"] == "validation"
            and a["state"] == "PASSED"
        ]
        if not any(key.startswith("attempt:") for key in deps):
            continue  # An unvalidated candidate is not an enabled input fact.
        step = max(facts[key].available_step for key in deps)
        add(
            f"skill:{row['skill_id']}:{row['revision']}",
            "skill",
            row["document_hash"],
            "declared-workflow-history",
            doc["scope"]["repo"],
            step,
            deps,
            executed=row["enabled_at"] or row["available_at"],
        )
    # No datum may claim availability before any supporting fact. Online episode
    # dependencies can include earlier history; topological closure raises its step.
    for _ in range(len(facts) + 1):
        changed = False
        for key, fact in list(facts.items()):
            if any(dep not in facts for dep in fact.dependencies):
                raise ValueError("memory_catalog_missing_provenance")
            step = max([fact.available_step, *(facts[d].available_step for d in fact.dependencies)])
            if step != fact.available_step:
                facts[key] = fact.model_copy(update={"available_step": step})
                changed = True
        if not changed:
            break
    return tuple(facts[key] for key in sorted(facts))


def eligible(facts, task):
    return tuple(f.fact_id for f in facts if f.available_step < task.step and f.source_instance != task.instance_id)
