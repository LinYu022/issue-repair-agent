"""Trusted source adapters: user confirmation, granted document reads and actual visible test evidence."""

import json
from dataclasses import asdict
from uuid import uuid4

from domain.models import ToolObservation, ToolStatus
from domain.plans import document_hash, path_subset
from domain.semantic_memory import MemoryScope, SemanticSource
from domain.workspace_tools import WorkspaceToolRequest
from tools.readonly_workspace import WorkspaceRefused
from tools.workspace_files import sha


def environment_fingerprint(session):
    return document_hash(
        {
            "environment_id": session.task.environment_id,
            "image_id": session.runner.image,
            "resources": asdict(session.runner.limits),
            "profiles": {k: v.model_dump(mode="json") for k, v in session.profiles.items()},
        }
    )


class SemanticIngestor:
    def __init__(self, store):
        self.store = store

    def confirmation(self, user_id, confirmation):
        source = SemanticSource(
            source_id=confirmation.source_id,
            user_id=user_id,
            origin="user_confirmation",
            scope=confirmation.scope,
            key=confirmation.key,
            quote=confirmation.text,
            dependencies=confirmation.dependencies,
            evidence={"verification": "authenticated_owner_confirmation", "claim": "preference_not_code_fact"},
        )
        return self.store.register_source(source)

    def inference(self, user_id, scope, key, text):
        source = SemanticSource(
            source_id=uuid4(),
            user_id=user_id,
            origin="model_inference",
            scope=scope,
            key=key,
            quote=text,
            evidence={"verification": "unverified_model_proposal"},
        )
        return self.store.register_source(source)

    @staticmethod
    def _record(loop, call_id):
        session = loop.session
        record, request, result = session.records.read(session.records.root / "calls/tool" / f"{call_id}.json")
        if (
            not isinstance(request, WorkspaceToolRequest)
            or not isinstance(result, ToolObservation)
            or record.evidence_kind != "real_execution"
            or result.status != ToolStatus.SUCCEEDED
        ):
            raise WorkspaceRefused("semantic_source_requires_successful_visible_workspace_call")
        if (request.task_id, request.workspace_id) != (session.task.task_id, session.identity.workspace_id):
            raise WorkspaceRefused("semantic_source_foreign_task")
        sentinel = loop._request({"operation": "diff", "path": "."})
        reason = session.stop_reason(sentinel)
        if reason:
            raise WorkspaceRefused(reason)
        if request.expected_version != session.task.workspace_version or session.files.snapshot() is None:
            raise WorkspaceRefused("semantic_source_historical_version_requires_fresh_observation")
        payload = json.loads(session.records.artifacts.read_text(result.artifact))
        if payload.get("late_result_discarded"):
            raise WorkspaceRefused("semantic_source_late_observation")
        return record, request, result, payload

    @staticmethod
    def _scope(loop, branch, paths, *, exact_workspace=False, watch=()):
        files = loop.session.files.snapshot()
        from tools.workspace_files import workspace_version

        if workspace_version(files) != loop.session.task.workspace_version:
            raise WorkspaceRefused("semantic_ingestion_external_workspace_change")
        names = set(files) if exact_workspace else set(watch)
        names.update({"pyproject.toml", "uv.lock", "requirements.txt", "setup.cfg", "tox.ini"})
        watched = {p: sha(files[p].encode()) if p in files else None for p in sorted(names)}
        return MemoryScope(
            repo=loop.session.identity.repo,
            branches=(branch,),
            commits=(loop.session.identity.base_commit,),
            paths=paths,
            environment_hash=environment_fingerprint(loop.session),
            workspace_version=loop.session.task.workspace_version if exact_workspace else None,
            watched_files=watched,
        )

    def document(self, loop, user_id, branch, call_id, key, quote):
        record, request, result, payload = self._record(loop, call_id)
        if request.arguments.operation != "read" or not request.arguments.path.endswith((".md", ".rst", ".txt")):
            raise WorkspaceRefused("semantic_convention_requires_visible_document_read")
        data = payload["result"]
        current = loop.session.files.snapshot().get(request.arguments.path)
        if not quote or quote not in data["text"] or current is None or sha(current.encode()) != data["sha256"]:
            raise WorkspaceRefused("semantic_document_quote_or_hash_mismatch")
        scope = self._scope(loop, branch, (".",), watch=(request.arguments.path,))
        grant = loop.controller.effective_grant(loop.session.grants[loop.session.task.grant_id])
        if not path_subset(tuple(scope.watched_files), grant.paths):
            raise WorkspaceRefused("semantic_source_watches_outside_current_grant")
        source = SemanticSource(
            source_id=uuid4(),
            user_id=user_id,
            origin="static_document",
            scope=scope,
            key=key,
            quote=quote,
            evidence={
                "verification": "descriptor_verified_quote",
                "task_id": str(request.task_id),
                "call_id": str(call_id),
                "record_hash": document_hash(record),
                "source_file": request.arguments.path,
                "source_file_sha256": data["sha256"],
                "observation_hash": document_hash(result),
                "truth_claim": "document_assertion_not_executed_fact",
            },
        )
        return self.store.register_source(source)

    def execution(self, loop, user_id, branch, call_id, key):
        record, request, observation, payload = self._record(loop, call_id)
        if request.arguments.operation != "run_test":
            raise WorkspaceRefused("semantic_command_requires_registered_test_profile")
        run = payload.get("run", {})
        process = json.loads((loop.session.runner.records / f"{call_id}.json").read_text())
        profile = loop.session.profiles[request.arguments.profile_id]
        context = payload.get("execution_context", {})
        if (
            context.get("profile_hash") != document_hash(profile)
            or context.get("image_id") != loop.session.runner.image
            or context.get("environment_id") != loop.session.task.environment_id
            or context.get("container_limits_hash") != document_hash(asdict(loop.session.runner.limits))
        ):
            raise WorkspaceRefused("semantic_command_environment_changed_since_observation")
        if (
            process["task_id"],
            process["call_id"],
            process["execution_epoch"],
            process["image_id"],
            process["profile_id"],
        ) != (
            str(request.task_id),
            str(call_id),
            request.execution_epoch,
            loop.session.runner.image,
            profile.profile_id,
        ):
            raise WorkspaceRefused("semantic_command_process_identity")
        if (
            not process["cleanup_confirmed"]
            or process["exit_code"] != 0
            or process["reason"]
            or run.get("output_capture") != process["output_capture"]
        ):
            raise WorkspaceRefused("semantic_command_not_confirmed_success")
        for stream, info in process["output_capture"].items():
            raw = (loop.session.runner.records / f"{call_id}.{stream}.bin").read_bytes()
            if sha(raw) != info["sha256"] or len(raw) != info["size_bytes"]:
                raise WorkspaceRefused("semantic_command_raw_stream_integrity")
        scope = self._scope(loop, branch, (".",), exact_workspace=True)
        source = SemanticSource(
            source_id=uuid4(),
            user_id=user_id,
            origin="visible_execution",
            scope=scope,
            key=key,
            quote=f"Registered profile {profile.profile_id} completed with exit code 0 on exactly the recorded workspace and environment; this is one visible execution.",
            evidence={
                "verification": "verified_visible_tool_and_raw_streams",
                "task_id": str(request.task_id),
                "call_id": str(call_id),
                "record_hash": document_hash(record),
                "observation_hash": document_hash(observation),
                "profile": profile.model_dump(mode="json"),
                "profile_hash": document_hash(profile),
                "process_hash": document_hash(process),
                "raw_streams": process["output_capture"],
                "exit_code": 0,
                "cleanup_confirmed": True,
            },
        )
        return self.store.register_source(source)
