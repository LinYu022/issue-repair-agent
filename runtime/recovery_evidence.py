"""Pure reconciliation rules and content-addressed evidence for interrupted calls."""

import json

from domain.checkpoints import FileChange, WorkspaceManifest, classify_changes
from domain.execution import ExecutionRefused
from domain.models import ArtifactRef
from domain.plans import document_hash
from runtime.call_records import CallRecordStore
from runtime.checkpoints import JointCheckpoints
from tools.workspace_files import sha, workspace_version


def capture_trace(root, objects):
    from types import SimpleNamespace

    collector = JointCheckpoints.__new__(JointCheckpoints)
    collector.session = SimpleNamespace(records=SimpleNamespace(root=root))
    collector.objects = objects
    return collector.capture_evidence()


def physical(manifest):
    return document_hash(
        {"entries": [e.model_dump(mode="json") for e in manifest.entries], "root_mode": manifest.root_mode}
    )


def recorded_call(call, trace):
    path = trace / "calls" / call["kind"] / f"{call['call_id']}.json"
    if not path.exists():
        return None
    record, request, result = CallRecordStore(trace).read(path)
    if (
        str(record.task_id) != str(call["task_id"])
        or str(record.call_id) != str(call["call_id"])
        or record.execution_epoch != call["execution_epoch"]
    ):
        raise ExecutionRefused("recovery_record_identity_mismatch")
    if request.model_dump(mode="json") != call["intent"]["request"]:
        raise ExecutionRefused("recovery_record_request_mismatch")
    if call["state"] in {"SUCCEEDED", "FAILED"} and record.status != call["state"]:
        raise ExecutionRefused("recovery_record_status_mismatch")
    if call["state"] in {"SUCCEEDED", "FAILED"}:
        saved = call["result"].get("observation" if call["kind"] == "tool" else "result")
        if saved is not None and document_hash(saved) != document_hash(result.model_dump(mode="json")):
            raise ExecutionRefused("recovery_ledger_result_record_mismatch")
    if call["operation"] == "run_test" and record.status in {"SUCCEEDED", "FAILED"}:
        process = json.loads((trace / "processes" / f"{call['call_id']}.json").read_text())
        if (
            not process["cleanup_confirmed"]
            or process["task_id"] != str(call["task_id"])
            or process["execution_epoch"] != call["execution_epoch"]
        ):
            raise ExecutionRefused("recovery_test_cleanup_unconfirmed")
        for name, ref in process["output_capture"].items():
            data = (trace / "processes" / f"{call['call_id']}.{name}.bin").read_bytes()
            if sha(data) != ref["sha256"] or len(data) != ref["size_bytes"]:
                raise ExecutionRefused("recovery_test_raw_stream_integrity")
    return record


def reconcile_tail(manifest, calls, actual, trace, objects):
    entries = {e.path: e for e in manifest.entries}
    resolutions = []
    for call in calls:
        state, kind, op = call["state"], call["kind"], call["operation"]
        previous = call.get("reconciliation")
        if previous and previous.get("reconciled"):
            verify_resolution(previous, objects)
        record = recorded_call(call, trace) if kind in {"tool", "model"} else None
        confirmed = record is not None and record.status in {"SUCCEEDED", "FAILED"}
        outcome = (
            previous["outcome"]
            if previous
            else "recorded_result"
            if confirmed
            else "not_dispatched"
            if state == "PREPARED"
            else ""
        )
        if state in {"SUCCEEDED", "FAILED"} and kind in {"tool", "model"} and not confirmed:
            raise ExecutionRefused("recovery_terminal_evidence_missing")
        if kind == "model" and not confirmed and outcome != "not_dispatched":
            raise ExecutionRefused("unknown_model_result_or_billing_no_automatic_retry")
        if kind == "tool" and op == "edit":
            changes = tuple(
                FileChange.model_validate(c) for c in call["intent"]["preconditions"].get("file_changes", [])
            )
            if not changes:
                raise ExecutionRefused("recovery_edit_preconditions_missing")
            if any(entries.get(c.path) != c.before for c in changes):
                raise ExecutionRefused("recovery_edit_chain_conflict")
            if confirmed:
                applied = record is not None and record.status == "SUCCEEDED"
            elif outcome == "edit_applied":
                applied = True
            elif state == "PREPARED" or outcome in {"not_dispatched", "edit_not_applied"}:
                applied = False
            else:
                if actual is None:
                    raise ExecutionRefused("unknown_edit_and_lost_workspace")
                states = {r["state"] for r in classify_changes(changes, actual)}
                if states == {"new"}:
                    applied = True
                    outcome = "edit_applied"
                elif states == {"old"}:
                    applied = False
                    outcome = "edit_not_applied"
                else:
                    raise ExecutionRefused("recovery_partial_edit_or_third_content_conflict")
            if applied:
                for change in changes:
                    if change.after:
                        entries[change.path] = change.after
                    else:
                        entries.pop(change.path, None)
        elif not outcome:
            if kind == "snapshot":
                outcome = "incomplete_checkpoint_ignored"
            elif kind == "tool" and op in {
                "read",
                "list",
                "search",
                "symbols",
                "investigate",
                "diff",
                "artifact_browse",
                "episode_memory",
                "run_test",
            }:
                outcome = "isolated_test_requires_fresh_validation" if op == "run_test" else "readonly_result_lost"
            else:
                raise ExecutionRefused("unclassified_external_side_effect_no_replay")
        resolutions.append(
            previous
            or {
                "call_id": str(call["call_id"]),
                "outcome": outcome,
                "prior_state": state,
                "reconciled": True,
                "no_task_containers": True,
                "record_hash": None if record is None else document_hash(record),
            }
        )
    ordered = tuple(sorted(entries.values(), key=lambda e: e.path))
    texts = {e.path: objects.read(e.content).decode("utf-8") for e in ordered if e.content}
    expected = WorkspaceManifest.model_validate(
        {**manifest.model_dump(), "entries": ordered, "content_version": workspace_version(texts)}
    )
    if actual is not None and physical(actual) != physical(expected):
        raise ExecutionRefused("workspace_human_change_or_partial_modification_conflict")
    proof = objects.put(expected.model_dump_json().encode())
    for resolution in resolutions:
        if "proof_manifest" not in resolution:
            resolution["proof_manifest"] = proof.model_dump(mode="json")
            resolution["proof_hash"] = document_hash(expected)
    return expected, tuple(resolutions)


def verify_resolution(resolution, objects):
    if not resolution or not resolution.get("reconciled") or not resolution.get("no_task_containers"):
        raise ValueError("checkpoint_unreconciled_unknown")
    if resolution.get("outcome") not in {
        "recorded_result",
        "not_dispatched",
        "edit_applied",
        "edit_not_applied",
        "incomplete_checkpoint_ignored",
        "readonly_result_lost",
        "isolated_test_requires_fresh_validation",
    }:
        raise ValueError("checkpoint_unknown_resolution_not_accepted")
    manifest = WorkspaceManifest.model_validate_json(
        objects.read(ArtifactRef.model_validate(resolution["proof_manifest"]))
    )
    if document_hash(manifest) != resolution["proof_hash"]:
        raise ValueError("checkpoint_recovery_proof_integrity")
    for entry in manifest.entries:
        if entry.content:
            objects.read(entry.content)
    return manifest
