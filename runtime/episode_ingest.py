"""Import closed, host-owned visible trajectories; evaluator data is never an input."""

import json
import tempfile
from contextlib import ExitStack
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid5

from domain.episodic_memory import CodeLocation, EpisodeDocument, EpisodeEvent, EpisodeRef
from domain.model_calls import ModelCallResult
from domain.models import ArtifactRef, ToolObservation
from domain.plans import document_hash
from domain.semantic_memory import MemoryScope, RevisionRef
from domain.workspace_tools import WorkspaceToolRequest
from runtime.diagnostic_evidence import DiagnosticEvidenceStore
from runtime.semantic_ingest import environment_fingerprint
from tools.workspace_files import sha, workspace_version


def episode_identity(user_id, task_id):
    return uuid5(user_id, "episode-v1:" + str(task_id))


class EpisodeIngestor:
    def __init__(self, store):
        self.store = store

    def capture(self, loop, user_id, branch, *, semantic_dependencies=(), fault=None):
        with loop.state_lock, loop.session.lock, ExitStack() as stack:
            session = loop.session
            owner = session.durable
            if owner is None or not loop.closed or loop.report is None or loop.report.get("task") is None:
                raise ValueError("episode_requires_closed_persistent_visible_loop")
            if not owner.closed:
                raise ValueError("episode_requires_released_source_owner")
            from runtime.execution_guard import ExclusiveFile

            for path in [owner.lock_path, owner.lock_path.with_suffix(".channel")]:
                lock = ExclusiveFile(path)
                stack.callback(lock.close)
            owner.require_no_containers()
            row = owner.store.read(session.task.task_id)
            if (
                not row["quiescent"]
                or row["worker_id"] != owner.lease.worker_id
                or row["lease_epoch"] != owner.lease.lease_epoch
            ):
                raise ValueError("episode_source_ownership_changed")
            if row["active_call_id"] is not None or row["payload"]["lifecycle"] == "RUNNING":
                raise ValueError("episode_source_task_still_active")
            files = session.files.snapshot()
            if workspace_version(files) != session.task.workspace_version:
                raise ValueError("episode_source_workspace_changed")
            if loop.report["task"] != session.task.model_dump(mode="json") or row["payload"] != loop.report["task"]:
                raise ValueError("episode_report_task_mismatch")
            evidence = DiagnosticEvidenceStore(session.records)
            evidence.root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="verify-episode-source-")))
            episode_id = episode_identity(user_id, session.task.task_id)
            events: list[EpisodeEvent] = []
            blobs = {}
            hypotheses = []

            def add(kind, label, text, source_hash, *, call_id=None, status=None, operation=None, location=None):
                sequence = len(events) + 1
                if sequence > 256:
                    raise ValueError("episode_event_limit_no_silent_truncation")
                # Full task-side artifacts remain in the source trace; the episode is a bounded view.
                raw = text.encode()
                truncated = len(raw) > 65536
                if truncated:
                    text = raw[:65536].decode("utf-8", errors="ignore")
                anchor_id = uuid5(episode_id, "anchor:" + str(sequence))
                ref = ArtifactRef(sha256=sha(text.encode()), size_bytes=len(text.encode()))
                events.append(
                    EpisodeEvent(
                        anchor_id=anchor_id,
                        sequence=sequence,
                        kind=kind,
                        label=label,
                        excerpt=text[:900],
                        text=ref,
                        text_truncated=truncated,
                        source_hash=source_hash,
                        call_id=call_id,
                        status=status,
                        operation=operation,
                        location=location,
                    )
                )
                blobs[str(anchor_id)] = text

            add("symptom", "source_issue_unverified", session.task.issue, document_hash(loop.package))
            for item in loop.events:
                kind = item["kind"]
                if kind == "plan_proposed":
                    path = loop.controller.records.root / "revisions" / f"{item['revision_id']}.json"
                    from domain.plans import PlanRevision

                    revision = PlanRevision.model_validate_json(path.read_text(encoding="utf-8"))
                    if revision.task_id != session.task.task_id or document_hash(revision) != item["revision_hash"]:
                        raise ValueError("episode_plan_source_integrity")
                    for hypothesis in revision.body.hypotheses:
                        if hypothesis not in hypotheses:
                            hypotheses.append(hypothesis)
                    add(
                        "hypothesis", "model_hypothesis_unverified", revision.model_dump_json(), document_hash(revision)
                    )
                elif kind == "strategy_decision":
                    add("strategy", "controller_strategy", json.dumps(item, ensure_ascii=False), document_hash(item))
                elif kind in {"tool_observation", "model_observation"}:
                    call_id = UUID(item["call_id"])
                    record, request, result = session.records.read(
                        session.records.root
                        / "calls"
                        / ("model" if kind == "model_observation" else "tool")
                        / f"{call_id}.json"
                    )
                    ledger = owner.store.call(session.task.task_id, call_id)
                    if (
                        ledger is None
                        or ledger["state"] != record.status
                        or ledger["intent"]["request"] != request.model_dump(mode="json")
                    ):
                        raise ValueError("episode_call_not_confirmed_in_ledger")
                    if record.task_id != session.task.task_id:
                        raise ValueError("episode_foreign_source_call")
                    if isinstance(result, ModelCallResult):
                        if result.reply is not None:
                            add(
                                "hypothesis",
                                "model_hypothesis_unverified",
                                result.reply.text,
                                document_hash(record),
                                call_id=call_id,
                                status=None,
                                operation="model_proposal",
                            )
                        continue
                    if not isinstance(result, ToolObservation):
                        raise ValueError("episode_visible_tool_record_required")
                    payload = json.loads(session.records.artifacts.read_text(result.artifact))
                    location = None
                    if isinstance(request, WorkspaceToolRequest):
                        evidence.capture(call_id)  # verifies original process streams and identities for tests
                        path = getattr(request.arguments, "path", None)
                        if path and path != ".":
                            location = CodeLocation(
                                path=path,
                                line=getattr(request.arguments, "start_line", None),
                                workspace_version=request.expected_version,
                            )
                        operation = request.arguments.operation
                    else:
                        operation = record.operation
                    add(
                        "tool",
                        "visible_tool_observation",
                        json.dumps(
                            {
                                "request": request.model_dump(mode="json"),
                                "observation": result.model_dump(mode="json"),
                                "payload": payload,
                            },
                            ensure_ascii=False,
                        ),
                        document_hash(record),
                        call_id=call_id,
                        status=record.status,
                        operation=operation,
                        location=location,
                    )
            if len(hypotheses) > 32:
                raise ValueError("episode_hypothesis_limit")
            outcome: Literal["self_test_passed", "failed", "paused", "cancelled", "unconfirmed"] = (
                "self_test_passed"
                if loop.report.get("completion_verified")
                else "failed"
                if session.task.lifecycle.value == "FAILED"
                else "cancelled"
                if session.task.lifecycle.value == "CANCELLED"
                else "paused"
                if session.task.lifecycle.value in {"PAUSED", "WAITING_INPUT", "WAITING_APPROVAL"}
                else "unconfirmed"
            )
            final = {
                "lifecycle": session.task.lifecycle.value,
                "self_test_status": session.task.self_test_status.value,
                "completion_verified": loop.report.get("completion_verified", False),
                "reason": loop.report.get("reason"),
                "tests": loop.tests,
                "independent_evaluation": "not_imported",
            }
            add("final", "visible_task_outcome", json.dumps(final, ensure_ascii=False), document_hash(final))
            baseline = {entry.path: entry.text for entry in loop.package.files}
            watched = {
                p: sha(baseline[p].encode()) if p in baseline else None
                for p in ["pyproject.toml", "uv.lock", "requirements.txt", "setup.cfg", "tox.ini", "setup.py"]
            }
            if any(files.get(p) != baseline.get(p) for p in watched):
                raise ValueError("episode_configuration_changed_during_source_task")
            scope = MemoryScope(
                repo=session.identity.repo,
                branches=(branch,),
                commits=(session.identity.base_commit,),
                paths=loop.registration.scope.read_paths,
                environment_hash=environment_fingerprint(session),
                watched_files=watched,
            )
            parents = {}
            semantic_refs = {str(p.memory_id): p for p in semantic_dependencies}
            if loop.context_manager.settings.memory_enabled:
                provider = loop.context_manager.memory
                if getattr(provider, "user_id", None) != user_id or getattr(provider, "branch", None) != branch:
                    raise ValueError("episode_memory_source_owner_binding")
                with self.store.semantic.transaction(user_id) as (c, _):
                    used = c.execute(
                        "SELECT payload FROM public.agent_memory_events WHERE user_id=%s AND payload->>'kind'='semantic_retrieved' AND payload->'details'->>'task_id'=%s",
                        (user_id, str(session.task.task_id)),
                    ).fetchall()
                    for event in used:
                        for ref in event["payload"]["details"]["items"]:
                            semantic_refs[ref["memory_id"]] = RevisionRef.model_validate(ref)
                    used = c.execute(
                        "SELECT DISTINCT episode_id,payload->>'document_hash' AS hash FROM public.agent_episode_usage WHERE user_id=%s AND consumer_task_id=%s AND kind IN ('recalled','read','declared_adoption')",
                        (user_id, session.task.task_id),
                    ).fetchall()
                    for event in used:
                        parents[str(event["episode_id"])] = EpisodeRef(
                            episode_id=event["episode_id"], document_hash=event["hash"]
                        )
            doc = EpisodeDocument(
                episode_id=episode_id,
                user_id=user_id,
                source_task_id=session.task.task_id,
                source_instance_id=session.task.source_task_id,
                scope=scope,
                source_initial_version=loop.initial_version,
                source_final_version=session.task.workspace_version,
                symptom=session.task.issue[:1600],
                hypotheses=tuple(hypotheses),
                outcome=outcome,
                outcome_reason=loop.report.get("reason", "unconfirmed"),
                counterevidence=tuple(e.anchor_id for e in events if e.status in {"FAILED", "UNKNOWN"}),
                events=tuple(events),
                semantic_dependencies=tuple(semantic_refs.values()),
                parent_episodes=tuple(parents.values()),
            )
            return self.store.publish(doc, blobs, fault=fault)
