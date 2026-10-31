"""Trusted single-host recovery supervisor. No model-callable recovery or approval endpoint."""

import json
import os
import tempfile
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4, uuid5

from domain.execution import ExecutionRefused, WorkerIdentity
from domain.models import ArtifactRef
from domain.plans import PlanRevision, document_hash
from domain.recovery import RecoveryDecision, RecoveryRequest, RecoveryReview
from domain.workspace_tools import TaskPackage
from domain.workspaces import WorkspaceIdentity
from runtime.checkpoint_archive import CheckpointArchive
from runtime.checkpoint_store import PostgresCheckpointStore
from runtime.checkpoints import normalized
from runtime.container_runner import ContainerLimits, ContainerRunner
from runtime.execution_guard import (
    DurableExecution,
    ExclusiveFile,
    process_identity,
    process_state,
    stop_registered_worker,
)
from runtime.recovery_evidence import capture_trace, physical, reconcile_tail
from runtime.recovery_graph import restore_graph
from runtime.recovery_store import RecoveryStore
from runtime.snapshot_objects import fsync_directory
from runtime.workspace_manifest import collect_workspace
from tools.workspace_files import WorkspaceFiles


def register_recovery(loop, parent: Path):
    """Explicit trusted assembly; the baseline registration remains unchanged."""
    parent = parent.resolve(strict=True)
    owner = loop.session.durable
    if owner is None or parent.is_relative_to(loop.session.files.root.resolve()):
        raise ExecutionRefused("recovery_parent_requires_separate_persistent_owner")
    with tempfile.TemporaryDirectory(prefix=".recovery-mode-probe-", dir=parent) as name:
        probe = Path(name) / "mode"
        probe.write_text("mode fidelity probe")
        os.chmod(probe, 0o640)
        if probe.stat().st_mode & 0o777 != 0o640:
            raise ExecutionRefused("recovery_parent_requires_posix_permission_fidelity")
    spec = normalized(
        {
            "identity": asdict(loop.session.identity),
            "package": loop.package,
            "registration": loop.registration,
            "provenance": loop.controller.provenance,
            "environment": {
                "environment_id": loop.session.task.environment_id,
                "image_id": loop.session.runner.image,
                "resources": asdict(loop.session.runner.limits),
                "profiles": {k: v.model_dump(mode="json") for k, v in loop.session.profiles.items()},
            },
        }
    )
    RecoveryStore(owner.store).register(loop, spec, parent)


def identity_from(spec):
    data = spec["identity"]
    return WorkspaceIdentity(UUID(data["task_id"]), UUID(data["workspace_id"]), data["repo"], data["base_commit"])


def authority_stamp(row):
    return document_hash(
        {
            "payload": row["payload"],
            "authority": row["authority"],
            "cancelled": row["cancelled"],
            "deadline": str(row["deadline"]),
        }
    )


def ownership_stamp(row):
    return document_hash(
        {k: row[k] for k in ("workspace_key", "lock_path", "worker_identity", "worker_id", "lease_epoch", "quiescent")}
    )


class RecoveryService:
    def __init__(self, execution, *, memory_bindings=None):
        self.execution, self.store = execution, RecoveryStore(execution)
        self.memory_bindings = dict(memory_bindings or {})

    def components(self, task_id):
        runtime = self.store.runtime(task_id)
        for key in ("workspace_path", "trace_path", "checkpoint_path", "lock_path", "recovery_parent"):
            path = Path(runtime[key])
            if not path.is_absolute() or path.resolve() != path:
                raise ExecutionRefused("recovery_registered_path_changed_or_symlink")
        environment = runtime["spec"]["environment"]
        runner = ContainerRunner(
            environment["image_id"],
            Path(runtime["trace_path"]) / "processes",
            ContainerLimits(**environment["resources"]),
        )
        archive = CheckpointArchive(PostgresCheckpointStore(self.execution, Path(runtime["checkpoint_path"])))
        return runtime, runner, archive

    def inventory(self, task_id, runner):
        row = self.execution.read(task_id)
        worker = (
            "none"
            if not row["worker_identity"]
            else process_state(WorkerIdentity.model_validate(row["worker_identity"]))
        )
        found = runner.docker(
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            "label=issue-agent.managed=p2-02",
            "--filter",
            f"label=issue-agent.task={task_id}",
        )
        if found.returncode:
            raise ExecutionRefused("recovery_process_inventory_unknown")
        containers = []
        for identifier in found.stdout.decode().split():
            result = runner.docker("container", "inspect", identifier)
            if result.returncode:
                raise ExecutionRefused("recovery_container_disappeared_reinspect")
            value = json.loads(result.stdout)[0]
            name = value["Name"].lstrip("/")
            matches = [
                c for c in self.store.calls(task_id) if c["process"] and c["process"].get("container_name") == name
            ]
            if len(matches) != 1:
                raise ExecutionRefused("recovery_unregistered_container_safe_stop")
            call = matches[0]
            labels = value["Config"]["Labels"]
            if (labels.get("issue-agent.task"), labels.get("issue-agent.epoch"), value["Image"]) != (
                str(task_id),
                str(call["execution_epoch"]),
                runner.image,
            ):
                raise ExecutionRefused("recovery_container_identity_mismatch")
            registered_id = call["process"].get("container_id")
            if registered_id and registered_id != identifier:
                raise ExecutionRefused("recovery_container_id_changed")
            containers.append(
                {"id": identifier, "name": name, "running": value["State"]["Running"], "call_id": str(call["call_id"])}
            )
        return {"worker": worker, "containers": containers, "lease_live": bool(row["lease_live"])}

    def quiesce(self, task_id, *, action):
        if action not in {"wait", "stop", "cancel"}:
            raise ExecutionRefused("recovery_quiesce_action")
        runtime, runner, _ = self.components(task_id)
        if action == "cancel":
            self.execution.cancel(task_id)  # Cancellation persists before signalling anything.
        before = self.inventory(task_id, runner)
        row = self.execution.read(task_id)
        if action in {"stop", "cancel"}:
            if before["worker"] == "alive":
                stop_registered_worker(WorkerIdentity.model_validate(row["worker_identity"]))
            elif before["worker"] not in {"none", "stopped", "different_process"}:
                raise ExecutionRefused("recovery_worker_identity_unknown")
            for container in before["containers"]:
                if not runner.remove(container["id"]):
                    raise ExecutionRefused("recovery_container_cleanup_unconfirmed")
        # wait never signals or treats the mere passage of time as success.
        after = self.inventory(task_id, runner)
        audit = {"action": action, "before": before, "after": after, "original_result_reconstructed": False}
        path = Path(runtime["trace_path"]) / "repair" / ("quiesce-" + uuid4().hex + ".json")
        path.write_text(json.dumps(audit, indent=2) + "\n")
        return audit

    def inspect(self, request: RecoveryRequest):
        old = self.store.begin(request)
        if old["review"] is not None:
            return RecoveryReview.model_validate(old["review"])
        try:
            review = self._inspect(request)
        except (ValueError, OSError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "recovery_evidence_unavailable"
            review = RecoveryReview(
                request=request,
                status="CONFLICT" if "conflict" in reason else "SAFE_STOPPED",
                reason=reason,
                stages=("authorization", "ownership", "processes", "checkpoint", "workspace", "environment"),
                original_authorization_state="untrusted_requires_fresh_approval",
            )
        return self.store.review(review)

    def _inspect(self, request):
        stages = []
        row = self.execution.read(request.task_id)
        authority = row["authority"]
        original = "active" if authority.get("authorization_active") else "inactive_or_revoked"
        if authority.get("authorization") and authority["authorization"].get("grant", {}).get("expires_at"):
            if datetime.fromisoformat(
                authority["authorization"]["grant"]["expires_at"].replace("Z", "+00:00")
            ) <= datetime.now(timezone.utc):
                original = "expired"
        stages.append("authorization")
        base = {"request": request, "stages": tuple(stages), "original_authorization_state": original}
        if row["cancelled"]:
            return RecoveryReview(**base, status="CANCELLED", reason="cancelled_task_cleanup_only")
        if not row["within_deadline"] or row["payload"]["lifecycle"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            return RecoveryReview(**base, status="SAFE_STOPPED", reason="terminal_or_original_deadline_exhausted")
        if authority.get("authorization") and original == "inactive_or_revoked":
            return RecoveryReview(
                **base, status="SAFE_STOPPED", reason="authorization_revoked_requires_operator_new_task_or_plan"
            )
        runtime, runner, archive = self.components(request.task_id)
        if runtime["active_recovery_id"] is not None:
            return RecoveryReview(
                **base, status="SAFE_STOPPED", reason="interrupted_recovery_requires_operator_audit_no_automatic_replay"
            )
        stages.append("ownership")
        inventory = self.inventory(request.task_id, runner)
        stages.append("processes")
        base["stages"] = tuple(stages)
        if (
            inventory["worker"] not in {"none", "stopped", "different_process"}
            or inventory["containers"]
            or inventory["lease_live"]
        ):
            return RecoveryReview(
                **base, status="WAITING_PROCESSES", reason="old_worker_container_or_lease_not_quiescent"
            )
        lifetime = ExclusiveFile(Path(runtime["lock_path"]))
        channel = None
        archive_lock = None
        try:
            channel = ExclusiveFile(Path(runtime["lock_path"]).with_suffix(".channel"))
            archive_lock = ExclusiveFile(archive.root / ".archive.lock")
            rows = archive.store.committed_rows(request.task_id)
            if not rows:
                raise ExecutionRefused("recovery_no_committed_checkpoint")
            document, manifest, graph = archive.load(rows[-1]["checkpoint_id"], request.task_id)
            memory = graph.context.get("memory_binding")
            if memory and self.memory_bindings.get(request.task_id) != memory:
                raise ExecutionRefused("recovery_semantic_memory_requires_fresh_trusted_binding")
            if graph.replanning["settings"]["enabled"]:
                past = len(graph.loop["events"])
                tail_policy = [
                    json.loads(p.read_text()) for p in (Path(runtime["trace_path"]) / "repair/events").glob("*.json")
                ]
                if any(
                    e["sequence"] > past and e["kind"] in {"strategy_decision", "replan_applied", "plan_proposed"}
                    for e in tail_policy
                ):
                    raise ExecutionRefused("recovery_tail_policy_requires_new_joint_checkpoint_or_operator_plan")
            stages.append("checkpoint")
            if graph.loop["closed"]:
                raise ExecutionRefused("recovery_checkpoint_loop_closed")
            calls = self.store.calls(request.task_id)
            by_id = {str(c["call_id"]): c for c in calls}
            for before in document.ledger["calls"]:
                current = normalized(by_id[before["call_id"]])
                if any(current[k] != before[k] for k in ("intent_hash", "result_hash", "state", "reconciliation")):
                    raise ExecutionRefused("recovery_checkpoint_ledger_changed")
            known = {c["call_id"] for c in document.ledger["calls"]} | {str(document.call_id)}
            tail = [c for c in calls if str(c["call_id"]) not in known]
            workspace = Path(runtime["workspace_path"])
            package = TaskPackage.model_validate(runtime["spec"]["package"])
            actual = (
                collect_workspace(
                    WorkspaceFiles(workspace),
                    identity_from(runtime["spec"]),
                    archive.objects,
                    [e.path for e in package.files],
                )
                if workspace.exists()
                else None
            )
            if actual is None and not request.rebuild_missing_workspace:
                raise ExecutionRefused("missing_workspace_requires_explicit_rebuild_request")
            if actual is not None:
                info = workspace.stat()
                key = document_hash({"path": str(workspace), "device": info.st_dev, "inode": info.st_ino})
                if row["workspace_key"] != key:
                    raise ExecutionRefused("recovery_workspace_root_replaced_conflict")
            expected, resolutions = reconcile_tail(manifest, tail, actual, Path(runtime["trace_path"]), archive.objects)
            for resolution in resolutions:
                resolution.setdefault("recovery_id", str(request.recovery_id))
            stages.append("workspace")
            if document.environment != runtime["spec"]["environment"]:
                raise ExecutionRefused("recovery_environment_registration_changed")
            image = runner.docker("image", "inspect", runner.image, "--format", "{{.Id}}")
            if image.returncode or image.stdout.decode().strip() != runner.image:
                raise ExecutionRefused("recovery_fixed_environment_unavailable")
            stages.append("environment")
            plan_data = graph.control["plan"]
            if plan_data is None:
                raise ExecutionRefused("recovery_requires_checkpoint_reviewable_plan")
            plan = PlanRevision.model_validate(
                {
                    **plan_data,
                    "revision_id": uuid5(request.recovery_id, "fresh-plan"),
                    "parent_revision_id": plan_data["revision_id"],
                    "linked_authorization_id": None,
                    "workspace_version": expected.content_version,
                    "created_at": datetime.now(timezone.utc),
                    "reason": "Recovery: re-read files, revalidate profiles and retain original limits",
                }
            )
            trace = capture_trace(Path(runtime["trace_path"]), archive.objects)
            return RecoveryReview(
                request=request,
                status="READY",
                reason="consistent_checkpoint_tail_and_fixed_environment_new_approval_required",
                stages=tuple(stages),
                original_authorization_state=original,
                checkpoint_id=document.checkpoint_id,
                source_fingerprint="missing" if actual is None else physical(actual),
                runtime_fingerprint=runtime["spec_hash"],
                ledger_fingerprint=document_hash({"calls": normalized(calls)}),
                authority_fingerprint=authority_stamp(row),
                ownership_fingerprint=ownership_stamp(row),
                trace_fingerprint=document_hash({"trace": trace}),
                trace_entries=trace,
                expected_manifest=expected.model_dump(mode="json"),
                new_plan=plan,
                resolutions=resolutions,
            )
        finally:
            if archive_lock:
                archive_lock.close()
            if channel:
                channel.close()
            lifetime.close()

    def apply(self, recovery_id, *, fault=None):
        """fault is a controller-only semantic crash hook, never a model/CLI input."""
        saved = self.store.run(recovery_id)
        if saved["state"] == "RESTORED":
            return saved["result"]
        if saved["state"] != "APPROVED":
            raise ExecutionRefused("recovery_not_approved_or_interrupted_requires_new_inspection")
        review = RecoveryReview.model_validate(saved["review"])
        decision = RecoveryDecision.model_validate(saved["decision"])
        if (
            decision.review_hash != document_hash(review)
            or review.new_plan is None
            or decision.plan_hash != document_hash(review.new_plan)
            or decision.decision != "approve"
        ):
            raise ExecutionRefused("recovery_decision_integrity")
        runtime, runner, archive = self.components(review.request.task_id)
        lifetime = ExclusiveFile(Path(runtime["lock_path"]))
        channel = None
        new_lock = None
        archive_lock = None
        adopted_owner = None
        lease = None
        try:
            channel = ExclusiveFile(Path(runtime["lock_path"]).with_suffix(".channel"))
            archive_lock = ExclusiveFile(archive.root / ".archive.lock")
            fresh = self._inspect_locked(review, runtime, runner, archive)
            if decision.expires_at <= datetime.now(timezone.utc):
                raise ExecutionRefused("recovery_decision_expired")
            identity = process_identity(os.getpid(), fresh["workspace_key"])
            lease = self.execution.claim(review.request.task_id, uuid4(), identity, seconds=60)
            self.store.reserve(lease, recovery_id)
            # Never overwrite the human workspace. A killed restore leaves a quarantined directory.
            target = Path(runtime["recovery_parent"]) / ("recovery-" + str(recovery_id))
            target.mkdir(mode=0o700)
            workspace, trace = target / "workspace", target / "trace"
            self.materialize(review, archive, workspace, trace)
            if fault:
                fault("after_materialized")
            info = workspace.stat()
            key = document_hash({"path": str(workspace), "device": info.st_dev, "inode": info.st_ino})
            lock_path = Path(runtime["lock_path"]).parent / (key + ".worker")
            new_lock = ExclusiveFile(lock_path)
            for resolution in review.resolutions:
                call = self.execution.call(lease.task_id, UUID(resolution["call_id"]))
                if call["state"] in {"PREPARED", "RUNNING", "UNKNOWN"}:
                    self.store.resolve(lease, call["call_id"], resolution)
            _, _, graph = archive.load(review.checkpoint_id, review.request.task_id)
            row = self.execution.read(review.request.task_id)
            loop = restore_graph(runtime["spec"], graph, row, review, decision, workspace, trace)
            if graph.context.get("memory_binding"):
                from runtime.semantic_provider import attach_semantic_memory

                binding = self.memory_bindings[review.request.task_id]
                # attach needs a live owner, so defer binding until after fenced activation.
            worker = process_identity(os.getpid(), key)
            lease = self.store.rebind(lease, recovery_id, worker, workspace, trace, lock_path, loop.session.task)
            if fault:
                fault("after_rebound")
            result = {
                "recovery_id": str(recovery_id),
                "task_id": str(lease.task_id),
                "workspace": str(workspace),
                "trace": str(trace),
                "checkpoint_id": str(review.checkpoint_id),
                "recovery_completed": True,
                "resolved": None,
                "lease_epoch": lease.lease_epoch,
                "execution_epoch": loop.session.task.execution_epoch,
                "original_deadline": str(row["deadline"]),
                "original_authority_reused": False,
            }
            self.store.activate(lease, recovery_id, loop.controller.view(), result)
            # Adopt the already fenced lease and held new lifetime lock; no second claim.
            owner = DurableExecution.__new__(DurableExecution)
            owner.store, owner.runner, owner.task_id = self.execution, loop.session.runner, lease.task_id
            owner.workspace, owner.workspace_key, owner.identity = workspace, key, worker
            owner.lock_path, owner.lifetime, owner.lease = lock_path, new_lock, lease
            owner.closed, owner.seconds, owner.session = False, 60, None
            owner.stop_event, owner.heartbeat_error, owner.thread = threading.Event(), None, None
            owner.runner.owner_lock_fd = new_lock.fd
            adopted_owner = owner
            new_lock = None
            owner.attach(loop)
            owner.snapshot_root = archive.root
            if graph.context.get("memory_binding"):
                from runtime.semantic_store import SemanticStore

                attach_semantic_memory(
                    loop,
                    SemanticStore(self.execution),
                    user_id=UUID(binding["user_id"]),
                    branch=binding["branch"],
                )
                if binding.get("episodic"):
                    from runtime.episode_provider import attach_episodic_memory
                    from runtime.episode_store import EpisodeStore

                    attach_episodic_memory(
                        loop,
                        SemanticStore(self.execution),
                        EpisodeStore(self.execution),
                        user_id=UUID(binding["user_id"]),
                        branch=binding["branch"],
                    )
                if binding.get("procedural"):
                    from runtime.skill_provider import attach_procedural_memory
                    from runtime.skill_store import SkillStore

                    attach_procedural_memory(
                        loop,
                        SemanticStore(self.execution),
                        EpisodeStore(self.execution),
                        SkillStore(self.execution),
                        user_id=UUID(binding["user_id"]),
                        branch=binding["branch"],
                    )
            return loop
        except BaseException:
            if adopted_owner is not None:
                adopted_owner.stop_event.set()
                if adopted_owner.thread:
                    adopted_owner.thread.join(timeout=4)
                adopted_owner.closed = True
                adopted_owner.runner.owner_lock_fd = None
                adopted_owner.lifetime.close()
            if lease is not None:
                try:
                    self.execution.release(lease)
                except ValueError:
                    pass
            raise
        finally:
            if archive_lock:
                archive_lock.close()
            if new_lock:
                new_lock.close()
            if channel:
                channel.close()
            lifetime.close()

    def _inspect_locked(self, review, runtime, runner, archive):
        row = self.execution.read(review.request.task_id)
        inventory = self.inventory(review.request.task_id, runner)
        if (
            inventory["worker"] not in {"none", "stopped", "different_process"}
            or inventory["containers"]
            or inventory["lease_live"]
        ):
            raise ExecutionRefused("recovery_process_or_owner_changed_after_review")
        if (
            row["cancelled"]
            or not row["within_deadline"]
            or authority_stamp(row) != review.authority_fingerprint
            or ownership_stamp(row) != review.ownership_fingerprint
        ):
            raise ExecutionRefused("recovery_authority_or_ownership_changed_after_review")
        calls = self.store.calls(review.request.task_id)
        if (
            runtime["spec_hash"] != review.runtime_fingerprint
            or document_hash({"calls": normalized(calls)}) != review.ledger_fingerprint
        ):
            raise ExecutionRefused("recovery_runtime_or_calls_changed_after_review")
        if (
            document_hash({"trace": capture_trace(Path(runtime["trace_path"]), archive.objects)})
            != review.trace_fingerprint
        ):
            raise ExecutionRefused("recovery_evidence_changed_after_review")
        path = Path(runtime["workspace_path"])
        actual = (
            collect_workspace(
                WorkspaceFiles(path),
                identity_from(runtime["spec"]),
                archive.objects,
                [e.path for e in TaskPackage.model_validate(runtime["spec"]["package"]).files],
            )
            if path.exists()
            else None
        )
        if ("missing" if actual is None else physical(actual)) != review.source_fingerprint:
            raise ExecutionRefused("recovery_workspace_changed_after_review")
        if actual is not None:
            info = path.stat()
            if document_hash({"path": str(path), "device": info.st_dev, "inode": info.st_ino}) != row["workspace_key"]:
                raise ExecutionRefused("recovery_workspace_root_replaced_after_review")
        rows = archive.store.committed_rows(review.request.task_id)
        if rows[-1]["checkpoint_id"] != review.checkpoint_id:
            raise ExecutionRefused("recovery_latest_checkpoint_changed")
        _, _, graph = archive.load(review.checkpoint_id, review.request.task_id)
        memory = graph.context.get("memory_binding")
        if memory and self.memory_bindings.get(review.request.task_id) != memory:
            raise ExecutionRefused("recovery_semantic_memory_requires_fresh_trusted_binding")
        image = runner.docker("image", "inspect", runner.image, "--format", "{{.Id}}")
        if image.returncode or image.stdout.decode().strip() != runner.image:
            raise ExecutionRefused("recovery_fixed_environment_unavailable")
        return row

    def materialize(self, review, archive, workspace, trace):
        from domain.checkpoints import WorkspaceManifest

        manifest = WorkspaceManifest.model_validate(review.expected_manifest)
        workspace.mkdir(mode=0o700)
        directories = sorted(
            (e for e in manifest.entries if e.kind == "directory"), key=lambda e: len(e.path.split("/"))
        )
        for entry in directories:
            (workspace / entry.path).mkdir(mode=0o700)
        for entry in manifest.entries:
            if entry.content:
                with (workspace / entry.path).open("xb") as stream:
                    stream.write(archive.objects.read(entry.content))
                    stream.flush()
                    os.fsync(stream.fileno())
                    getattr(os, "fchmod")(stream.fileno(), entry.mode)
        for entry in reversed(directories):
            os.chmod(workspace / entry.path, entry.mode)
        os.chmod(workspace, manifest.root_mode)
        document, _, _ = archive.load(review.checkpoint_id, review.request.task_id)
        for item in document.evidence:
            path = trace / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.objects.read(ArtifactRef.model_validate(item["object"])))
        for item in review.trace_entries:
            # Tail messages and queued actions are audit only; do not resume their sequence counters.
            name = item["path"]
            if name.startswith("repair/") or name.startswith("context/") and not name.startswith("context/sources/"):
                name = "repair/recovery-tail/" + name
            path = trace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.objects.read(ArtifactRef.model_validate(item["object"])))
        actual = collect_workspace(
            WorkspaceFiles(workspace),
            identity_from(self.store.runtime(review.request.task_id)["spec"]),
            archive.objects,
            [e.path for e in manifest.entries if e.tracked],
        )
        if physical(actual) != physical(manifest):
            raise ExecutionRefused("recovery_materialization_verification_failed")
        fsync_directory(workspace)
