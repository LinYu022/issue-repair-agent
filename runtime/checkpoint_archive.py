"""Complete checkpoint loading, fresh inspection copies and serialized orphan-object reclamation."""

import json
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

from domain.checkpoints import CheckpointDocument, JointGraphState, WorkspaceManifest
from domain.models import ArtifactRef
from domain.plans import document_hash
from runtime.execution_guard import ExclusiveFile
from runtime.snapshot_objects import SnapshotObjects, fsync_directory

EVIDENCE_ROOTS = frozenset(
    {"calls", "objects", "planning", "context", "diagnosis", "investigations", "repair", "processes"}
)


def edit_references(calls):
    result = {}
    for call in calls:
        resolution = call.get("reconciliation") or {}
        if resolution.get("proof_manifest"):
            ref = ArtifactRef.model_validate(resolution["proof_manifest"])
            result[ref.sha256] = ref
        for change in call["intent"]["preconditions"].get("file_changes", []):
            for key in ("before", "after"):
                if change[key] and change[key].get("content"):
                    ref = ArtifactRef.model_validate(change[key]["content"])
                    result[ref.sha256] = ref
    return tuple(result.values())


class CheckpointArchive:
    def __init__(self, store):
        self.store = store
        self.root = store.root
        self.objects = SnapshotObjects(self.root / "objects")

    def verify_document(self, document):
        workspace = WorkspaceManifest.model_validate_json(self.objects.read(document.workspace))
        graph = JointGraphState.model_validate_json(self.objects.read(document.graph))
        if (workspace.task_id, workspace.workspace_id, graph.task_id) != (
            document.task_id,
            document.workspace_id,
            document.task_id,
        ):
            raise ValueError("checkpoint_object_identity_mismatch")
        if (
            graph.task["workspace_version"] != workspace.content_version
            or graph.task["execution_epoch"] != document.execution_epoch
        ):
            raise ValueError("checkpoint_graph_workspace_mismatch")
        if graph.control["task"] != graph.task:
            raise ValueError("checkpoint_graph_control_task_mismatch")
        texts = {}
        for entry in workspace.entries:
            if entry.content:
                data = self.objects.read(entry.content)
                text = data.decode("utf-8")
                if "\0" in text:
                    raise ValueError("checkpoint_binary_workspace_unsupported")
                texts[entry.path] = text
        from tools.workspace_files import workspace_version

        if workspace_version(texts) != workspace.content_version:
            raise ValueError("checkpoint_content_version_mismatch")
        paths = set()
        for item in document.evidence:
            from domain.workspaces import relative_parts

            parts = relative_parts(item["path"])
            if parts[0] not in EVIDENCE_ROOTS or item["path"] in paths:
                raise ValueError("checkpoint_evidence_path_not_registered")
            paths.add(item["path"])
            self.objects.read(ArtifactRef.model_validate(item["object"]))
        for ref in document.edit_objects:
            self.objects.read(ref)
        for change in document.changes:
            for entry in (change.before, change.after):
                if entry and entry.content:
                    self.objects.read(entry.content)
        if document_hash({"calls": document.ledger["calls"]}) != document.ledger["calls_hash"]:
            raise ValueError("checkpoint_ledger_cursor_mismatch")
        for call in document.ledger["calls"]:
            if call["task_id"] != str(document.task_id) or call["state"] not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
                raise ValueError("checkpoint_contains_unreconciled_call")
            if call["state"] == "UNKNOWN":
                from runtime.recovery_evidence import verify_resolution

                proof = verify_resolution(call.get("reconciliation"), self.objects)
                if proof.task_id != document.task_id:
                    raise ValueError("checkpoint_foreign_recovery_proof")
                from runtime.recovery_store import RecoveryStore

                resolution = call["reconciliation"]
                from uuid import UUID

                recovery = RecoveryStore(self.store.execution).run(UUID(resolution["recovery_id"]))
                if (
                    recovery["task_id"] != document.task_id
                    or recovery["state"] != "RESTORED"
                    or resolution not in recovery["review"]["resolutions"]
                ):
                    raise ValueError("checkpoint_recovery_proof_not_committed")
            if document_hash(call["intent"]) != call["intent_hash"]:
                raise ValueError("checkpoint_intent_hash_mismatch")
            if call["result"] is not None and document_hash(call["result"]) != call["result_hash"]:
                raise ValueError("checkpoint_result_hash_mismatch")
        self.verify_evidence(document)
        return workspace, graph

    def verify_evidence(self, document):
        from domain.call_records import CallRecord
        from domain.model_calls import ModelCallResult
        from domain.models import ToolObservation
        from tools.workspace_files import sha

        values = {
            item["path"]: self.objects.read(ArtifactRef.model_validate(item["object"])) for item in document.evidence
        }

        def object_data(ref, prefix="objects"):
            ref = ArtifactRef.model_validate(ref)
            data = values.get(prefix + "/" + ref.sha256 + ".txt")
            if data is None or len(data) != ref.size_bytes or sha(data) != ref.sha256:
                raise ValueError("checkpoint_required_evidence_object_missing")
            return data

        common = {}
        for path, data in values.items():
            if path.startswith("calls/") and path.endswith(".json"):
                record = CallRecord.model_validate_json(data)
                if record.task_id != document.task_id:
                    raise ValueError("checkpoint_foreign_call_evidence")
                request = json.loads(object_data(record.request_artifact))
                result = (
                    ModelCallResult.model_validate_json(object_data(record.result_artifact))
                    if record.call_kind == "model"
                    else ToolObservation.model_validate_json(object_data(record.result_artifact))
                )
                if isinstance(result, ToolObservation):
                    object_data(result.artifact)
                common[str(record.call_id)] = (record, request, result)
            elif path.startswith("context/sources/"):
                item = json.loads(data)
                from domain.context import ArtifactSource

                if item["source_hash"] != document_hash(ArtifactSource.model_validate(item["source"])) or item[
                    "source"
                ]["task_id"] != str(document.task_id):
                    raise ValueError("checkpoint_context_source_integrity")
                object_data(item["source"]["artifact"])
        for call in document.ledger["calls"]:
            if call["kind"] in {"tool", "model"}:
                reconciled = call["state"] == "UNKNOWN" and call.get("reconciliation", {}).get("reconciled")
                if call["call_id"] not in common:
                    if reconciled:
                        continue
                    raise ValueError("checkpoint_terminal_call_evidence_missing")
                record, request, result = common[call["call_id"]]
                if (not reconciled and record.status != call["state"]) or record.execution_epoch != call[
                    "execution_epoch"
                ]:
                    raise ValueError("checkpoint_call_ledger_mismatch")
                if request.get("arguments", {}).get("operation") == "run_test":
                    process_path = "processes/" + call["call_id"] + ".json"
                    if process_path not in values:
                        raise ValueError("checkpoint_test_process_record_missing")
                    process = json.loads(values[process_path])
                    if not process["cleanup_confirmed"]:
                        raise ValueError("checkpoint_unconfirmed_process")
                    for name, ref in process["output_capture"].items():
                        raw = values.get("processes/" + call["call_id"] + "." + name + ".bin")
                        if raw is None or len(raw) != ref["size_bytes"] or sha(raw) != ref["sha256"]:
                            raise ValueError("checkpoint_raw_stream_missing")

    def load(self, checkpoint_id, task_id):
        row = self.store.row(checkpoint_id)
        if row["state"] != "COMMITTED" or row["task_id"] != task_id:
            raise ValueError("only_committed_same_task_checkpoint_is_loadable")
        reference = ArtifactRef.model_validate(row["document"])
        document = CheckpointDocument.model_validate_json(self.objects.read(reference))
        if (
            document.checkpoint_id,
            document.task_id,
            document.call_id,
            document.lease_epoch,
            document.execution_epoch,
        ) != (
            row["checkpoint_id"],
            row["task_id"],
            row["call_id"],
            row["lease_epoch"],
            row["execution_epoch"],
        ) or document_hash(document) != row["document_hash"]:
            raise ValueError("checkpoint_index_document_mismatch")
        workspace, graph = self.verify_document(document)
        return document, workspace, graph

    def export_copy(self, checkpoint_id, task_id, parent: Path):
        parent = parent.resolve(strict=True)
        if parent.is_relative_to(self.root) or self.root.is_relative_to(parent / "workspace"):
            raise ValueError("inspection_export_requires_separate_parent")
        lock = ExclusiveFile(self.root / ".archive.lock")
        target = parent / ("checkpoint-copy-" + uuid4().hex)
        try:
            document, workspace, graph = self.load(checkpoint_id, task_id)
            target.mkdir(mode=0o700)
            output = target / "workspace"
            output.mkdir(mode=0o700)
            directories = sorted(
                (e for e in workspace.entries if e.kind == "directory"), key=lambda e: len(e.path.split("/"))
            )
            for entry in directories:
                (output / entry.path).mkdir(mode=0o700)
            for entry in workspace.entries:
                if entry.kind != "file":
                    continue
                path = output / entry.path
                with path.open("xb") as stream:
                    stream.write(self.objects.read(entry.content))
                    stream.flush()
                    os.fsync(stream.fileno())
                    getattr(os, "fchmod")(stream.fileno(), entry.mode)
            for entry in reversed(directories):
                os.chmod(output / entry.path, entry.mode)
                fsync_directory(output / entry.path)
            os.chmod(output, workspace.root_mode)
            fsync_directory(output)
            for item in document.evidence:
                path = target / "trace" / item["path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(self.objects.read(ArtifactRef.model_validate(item["object"])))
            (target / "graph.json").write_text(graph.model_dump_json(indent=2) + "\n", encoding="utf-8")
            (target / "manifest.json").write_text(workspace.model_dump_json(indent=2) + "\n", encoding="utf-8")
            # Verify the physical reconstruction, not merely the input object hashes.
            from domain.workspaces import WorkspaceIdentity
            from runtime.workspace_manifest import collect_workspace
            from tools.workspace_files import WorkspaceFiles

            actual = collect_workspace(
                WorkspaceFiles(output),
                WorkspaceIdentity(task_id, workspace.workspace_id, workspace.repo, workspace.base_commit),
                self.objects,
                [e.path for e in workspace.entries if e.tracked],
            )
            if (actual.entries, actual.root_mode, actual.content_version) != (
                workspace.entries,
                workspace.root_mode,
                workspace.content_version,
            ):
                raise ValueError("inspection_copy_metadata_or_content_mismatch")
            result = {
                "checkpoint_id": str(checkpoint_id),
                "workspace": str(output),
                "trace": str(target / "trace"),
                "content_version": actual.content_version,
                "authority_restored": False,
                "live_task_modified": False,
                "purpose": "verified_fresh_inspection_copy_only",
            }
            (target / "export.json").write_text(json.dumps(result, indent=2) + "\n")
            return result
        except BaseException:
            if target.exists():
                target.resolve().relative_to(parent)
                shutil.rmtree(target)
            raise
        finally:
            lock.close()

    def gc(self, task_id, *, minimum_age_seconds=3600):
        if type(minimum_age_seconds) is not int or minimum_age_seconds < 0:
            raise ValueError("bounded_nonnegative_gc_age_required")
        lock = ExclusiveFile(self.root / ".archive.lock")
        try:
            with self.store.execution.connection() as c:
                identities = {
                    r[0]
                    for r in c.execute(
                        "SELECT DISTINCT task_id FROM public.agent_checkpoints WHERE store_path=%s", (str(self.root),)
                    ).fetchall()
                }
            if identities != {task_id}:
                raise ValueError("gc_task_and_store_namespace_mismatch")
            keep = set()
            for row in self.store.committed_rows():
                document, workspace, graph = self.load(row["checkpoint_id"], row["task_id"])
                keep.update(
                    [
                        ArtifactRef.model_validate(row["document"]).sha256,
                        document.workspace.sha256,
                        document.graph.sha256,
                    ]
                )
                keep.update(e.content.sha256 for e in workspace.entries if e.content)
                keep.update(ArtifactRef.model_validate(i["object"]).sha256 for i in document.evidence)
                keep.update(r.sha256 for r in document.edit_objects)
                keep.update(e.content.sha256 for c in document.changes for e in (c.before, c.after) if e and e.content)
            with self.store.execution.connection() as c:
                calls = [
                    {"intent": r[0], "reconciliation": r[1]}
                    for r in c.execute(
                        "SELECT intent,reconciliation FROM public.agent_execution_calls WHERE task_id=%s", (task_id,)
                    ).fetchall()
                ]
            keep.update(r.sha256 for r in edit_references(calls))
            for call in calls:
                resolution = call.get("reconciliation")
                if resolution and resolution.get("reconciled"):
                    from runtime.recovery_evidence import verify_resolution

                    proof = verify_resolution(resolution, self.objects)
                    keep.update(e.content.sha256 for e in proof.entries if e.content)
            with self.store.execution.connection() as c:
                if c.execute("SELECT to_regclass('public.agent_recovery_runs')").fetchone()[0]:
                    reviews = c.execute(
                        "SELECT review FROM public.agent_recovery_runs WHERE task_id=%s AND review IS NOT NULL",
                        (task_id,),
                    ).fetchall()
                    for (review,) in reviews:
                        keep.update(
                            ArtifactRef.model_validate(i["object"]).sha256 for i in review.get("trace_entries", [])
                        )
                        if review.get("expected_manifest"):
                            keep.update(
                                e["content"]["sha256"]
                                for e in review["expected_manifest"]["entries"]
                                if e.get("content")
                            )
                        keep.update(
                            i["proof_manifest"]["sha256"]
                            for i in review.get("resolutions", [])
                            if i.get("proof_manifest")
                        )
            removed = []
            for path in self.objects.root.glob("*"):
                if path.is_symlink() or not path.is_file():
                    raise ValueError("unexpected_snapshot_object_entry")
                if minimum_age_seconds > 0 and time.time() - path.stat().st_mtime < minimum_age_seconds:
                    continue
                if path.name.endswith(".blob") and path.stem not in keep or path.name.startswith(".pending-"):
                    path.unlink()
                    removed.append(path.name)
            fsync_directory(self.objects.root)
            return {
                "removed_objects": removed,
                "retained_reference_hashes": len(keep),
                "minimum_age_seconds": minimum_age_seconds,
                "committed_checkpoints_preserved": True,
            }
        finally:
            lock.close()
