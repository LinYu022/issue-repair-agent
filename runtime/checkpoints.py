"""Capture the synchronous graph and complete workspace at the same fenced decision boundary."""

import json
import stat
import time
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel

from domain.checkpoints import CheckpointDocument, JointGraphState, manifest_changes
from domain.plans import document_hash
from runtime.checkpoint_archive import EVIDENCE_ROOTS, CheckpointArchive, edit_references
from runtime.checkpoint_store import PostgresCheckpointStore
from runtime.execution_guard import ExclusiveFile
from runtime.workspace_manifest import collect_workspace


def normalized(value):
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            default=lambda v: v.model_dump(mode="json")
            if isinstance(v, BaseModel)
            else list(v)
            if isinstance(v, (tuple, set, frozenset))
            else str(v),
        )
    )


class JointCheckpoints:
    def __init__(self, loop):
        self.loop, self.session = loop, loop.session
        self.owner = self.session.durable
        if self.owner is None:
            raise ValueError("joint_checkpoints_require_persistent_owner")
        self.store = PostgresCheckpointStore(self.owner.store, self.owner.snapshot_root)
        self.archive = CheckpointArchive(self.store)
        self.objects = self.archive.objects

    def graph(self):
        loop = self.loop
        context = loop.context_manager
        policy = loop.replanner
        loop_fields = [
            "initial_version",
            "messages",
            "events",
            "reads",
            "tests",
            "baseline_failure",
            "preflight_done",
            "invalid_action_count",
            "model_action_feedback",
            "closed",
            "report",
        ]
        policy_fields = [
            "settings",
            "facts",
            "expectations",
            "pending",
            "retries",
            "refusals",
            "corrections",
            "decisions",
            "revisions",
            "pin",
            "needs_plan",
            "investigated",
            "require_readonly",
            "initial_baselines_scheduled",
            "hypothesis",
            "last_classification",
            "last_evidence",
            "trigger_evidence",
            "model_call_id",
            "decision",
            "profiles",
        ]
        # Explicit allowlist: no DSN, credentials, lock FDs, live adapters or host paths.
        policy_values = {k: list(getattr(policy, k)) if k == "pending" else getattr(policy, k) for k in policy_fields}
        policy_values["repeat_detector"] = {
            "seen": policy.detector.seen,
            "distinct_observations": policy.detector.distinct_observations,
            "evidence_epoch": policy.detector.evidence_epoch,
        }
        data = {
            "task_id": self.session.task.task_id,
            "task": self.session.task.model_dump(mode="json"),
            "control": loop.controller.view(),
            "loop": {
                **{k: getattr(loop, k) for k in loop_fields},
                "registration": loop.registration,
                "package": loop.package,
            },
            "replanning": policy_values,
            "context": {
                **{
                    k: getattr(context, k)
                    for k in [
                        "settings",
                        "transcript",
                        "observations",
                        "events",
                        "builds",
                        "build_attempts",
                        "initial_read_scope",
                        "pages",
                    ]
                },
                **({"memory_binding": context.memory.binding()} if context.settings.memory_enabled else {}),
            },
            "elapsed_wall_seconds": max(0, time.monotonic() - self.session.started),
        }
        return JointGraphState.model_validate(normalized(data))

    def capture_evidence(self):
        references: list[dict] = []
        total = 0
        for name in sorted(EVIDENCE_ROOTS):
            directory = self.session.records.root / name
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*")):
                if path.is_symlink():
                    raise ValueError("checkpoint_trace_symlink_unsupported")
                if path.is_dir():
                    continue
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("checkpoint_trace_regular_files_required")
                total += info.st_size
                if len(references) >= 8192 or total > 64 * 1024 * 1024:
                    raise ValueError("checkpoint_evidence_limit")
                data = path.read_bytes()
                after = path.stat()
                if (info.st_mtime_ns, info.st_ctime_ns, info.st_size) != (
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                    len(data),
                ):
                    raise ValueError("checkpoint_trace_changed_during_capture")
                references.append(
                    {
                        "path": str(path.relative_to(self.session.records.root)).replace("\\", "/"),
                        "object": self.objects.put(data).model_dump(mode="json"),
                    }
                )
        return tuple(references)

    def create(self, *, checkpoint_id=None, fault=None):
        """fault is an explicit controller-test callback, never a model or public CLI field."""
        if not self.loop.state_lock.acquire(blocking=False):
            raise ValueError("checkpoint_graph_step_active")
        if not self.session.lock.acquire(blocking=False):
            self.loop.state_lock.release()
            raise ValueError("checkpoint_workspace_call_active")
        if not self.loop.controller.state_lock.acquire(blocking=False):
            self.session.lock.release()
            self.loop.state_lock.release()
            raise ValueError("checkpoint_control_transition_active")
        lock = None
        try:
            self.owner.check()
            self.owner.require_no_containers()
            lock = ExclusiveFile(self.archive.root / ".archive.lock")
            checkpoint_id = checkpoint_id or uuid4()
            call_id = uuid4()
            intent = self.owner.intent("snapshot", call_id, "joint_checkpoint", {"checkpoint_id": str(checkpoint_id)})
            with self.owner.channel(intent):
                boundary = self.store.ledger_boundary(self.owner.lease)
                boundary["calls"] = [c for c in boundary["calls"] if c["call_id"] != str(call_id)]
                boundary["calls_hash"] = document_hash({"calls": boundary["calls"]})
                stamp = document_hash({"task": boundary["task"], "authority": boundary["authority"]})
                self.store.begin(
                    self.owner.lease,
                    checkpoint_id,
                    call_id,
                    {"control_stamp": stamp, "workspace_version": self.session.task.workspace_version},
                )
                if fault:
                    fault("after_preparing", checkpoint_id)
                workspace = collect_workspace(
                    self.session.files, self.session.identity, self.objects, self.session.files.baseline
                )
                if workspace.content_version != self.session.task.workspace_version:
                    raise ValueError("checkpoint_current_workspace_changed")
                graph = self.graph()
                graph_stamp = document_hash(graph.model_copy(update={"elapsed_wall_seconds": 0}))
                workspace_ref = self.objects.put(workspace.model_dump_json().encode())
                graph_ref = self.objects.put(graph.model_dump_json().encode())
                evidence = self.capture_evidence()
                edit_objects = edit_references(boundary["calls"])
                rows = self.store.committed_rows(self.session.task.task_id)
                previous = None
                changes = renames = ()
                if rows:
                    previous = rows[-1]["checkpoint_id"]
                    _, old, _ = self.archive.load(previous, self.session.task.task_id)
                    changes, renames = manifest_changes(old, workspace)
                document = CheckpointDocument(
                    checkpoint_id=checkpoint_id,
                    task_id=self.session.task.task_id,
                    workspace_id=self.session.identity.workspace_id,
                    call_id=call_id,
                    lease_epoch=self.owner.lease.lease_epoch,
                    execution_epoch=self.session.task.execution_epoch,
                    workspace=workspace_ref,
                    graph=graph_ref,
                    evidence=evidence,
                    edit_objects=edit_objects,
                    environment={
                        "environment_id": self.session.task.environment_id,
                        "image_id": self.session.runner.image,
                        "resources": asdict(self.session.runner.limits),
                        "profiles": {k: v.model_dump(mode="json") for k, v in self.session.profiles.items()},
                    },
                    ledger=boundary,
                    control_stamp=stamp,
                    previous_checkpoint_id=previous,
                    changes=changes,
                    inferred_renames=renames,
                    created_at=datetime.now(timezone.utc),
                )
                reference = self.objects.put(document.model_dump_json().encode())
                if fault:
                    fault("objects_written_before_commit", checkpoint_id)
                self.archive.verify_document(document)

                def validate():
                    current = collect_workspace(
                        self.session.files, self.session.identity, self.objects, self.session.files.baseline
                    )
                    if current != workspace:
                        raise ValueError("checkpoint_workspace_changed_before_commit")
                    if document_hash(self.graph().model_copy(update={"elapsed_wall_seconds": 0})) != graph_stamp:
                        raise ValueError("checkpoint_graph_changed_before_commit")

                result = {
                    "checkpoint_id": str(checkpoint_id),
                    "checkpoint_committed": True,
                    "document": reference.model_dump(mode="json"),
                    "content_version": workspace.content_version,
                    "authority_restored": False,
                }
                self.store.commit(self.owner.lease, document, reference, result, validate)
                if fault:
                    fault("after_committed", checkpoint_id)
                return result
        finally:
            if lock:
                lock.close()
            self.session.lock.release()
            self.loop.controller.state_lock.release()
            self.loop.state_lock.release()
