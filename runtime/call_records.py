"""Validated post-call tracing; not a durable intent or recovery ledger."""

from datetime import datetime, timezone
from pathlib import Path

from domain.call_records import CallRecord
from domain.context import ArtifactBrowseRequest
from domain.episodic_memory import EpisodeToolRequest
from domain.model_calls import ModelCallResult, ModelRequest
from domain.models import ToolObservation
from domain.tasks import Task
from domain.tool_calls import ToolRequest
from domain.workspace_tools import WorkspaceToolRequest
from runtime.artifacts import LocalArtifactStore


class CallRecordStore:
    def __init__(self, root: Path):
        self.root = root
        self.artifacts = LocalArtifactStore(root / "objects")

    @staticmethod
    def _identity(request, result) -> dict:
        if isinstance(request, ModelRequest) and isinstance(result, ModelCallResult):
            identity = {
                "call_kind": "model",
                "task_id": request.task_id,
                "call_id": request.request_id,
                "operation": result.model_name,
            }
            result_id = result.request_id
        elif isinstance(
            request, (ToolRequest, WorkspaceToolRequest, ArtifactBrowseRequest, EpisodeToolRequest)
        ) and isinstance(result, ToolObservation):
            identity = {
                "call_kind": "tool",
                "task_id": request.task_id,
                "call_id": request.call_id,
                "operation": request.tool_name,
            }
            result_id = result.call_id
            if result.tool_name != request.tool_name:
                raise ValueError("request/result tool names do not match")
        else:
            raise ValueError("request/result types do not match")
        if result.task_id != request.task_id or result_id != identity["call_id"]:
            raise ValueError("request/result identities do not match")
        return identity

    def save(
        self,
        task: Task,
        request: ModelRequest | ToolRequest | WorkspaceToolRequest | ArtifactBrowseRequest | EpisodeToolRequest,
        result: ModelCallResult | ToolObservation,
    ) -> CallRecord:
        identity = self._identity(request, result)
        if task.task_id != identity["task_id"]:
            raise ValueError("call belongs to another task")
        if isinstance(result, ToolObservation):
            self.artifacts.read_text(result.artifact)
        record = CallRecord.model_validate(
            {
                **identity,
                "status": result.status.value,
                "evidence_kind": result.evidence_kind,
                "recorded_at": datetime.now(timezone.utc),
                "request_artifact": self.artifacts.put_text(request.model_dump_json(indent=2)),
                "result_artifact": self.artifacts.put_text(result.model_dump_json(indent=2)),
                "grant_id": request.grant_id
                if isinstance(request, (WorkspaceToolRequest, ArtifactBrowseRequest, EpisodeToolRequest))
                else task.grant_id,
                "revision_id": request.revision_id
                if isinstance(request, (WorkspaceToolRequest, ArtifactBrowseRequest, EpisodeToolRequest))
                else task.revision_id,
                "workspace_version": task.workspace_version,
                "execution_epoch": request.execution_epoch
                if isinstance(request, (WorkspaceToolRequest, ArtifactBrowseRequest, EpisodeToolRequest))
                else task.execution_epoch,
            }
        )
        path = self.root / "calls" / record.call_kind / f"{record.call_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as file:
            file.write(record.model_dump_json(indent=2) + "\n")
        return record

    def read(
        self, path: Path
    ) -> tuple[
        CallRecord,
        ModelRequest | ToolRequest | WorkspaceToolRequest | ArtifactBrowseRequest | EpisodeToolRequest,
        ModelCallResult | ToolObservation,
    ]:
        record = CallRecord.model_validate_json(path.read_text(encoding="utf-8"))
        request_type, result_type = (
            (ModelRequest, ModelCallResult)
            if record.call_kind == "model"
            else (
                WorkspaceToolRequest
                if record.operation.startswith("workspace_")
                else ArtifactBrowseRequest
                if record.operation == "artifact_browse"
                else EpisodeToolRequest
                if record.operation == "episode_memory"
                else ToolRequest,
                ToolObservation,
            )
        )
        request = request_type.model_validate_json(self.artifacts.read_text(record.request_artifact))
        result = result_type.model_validate_json(self.artifacts.read_text(record.result_artifact))
        identity = self._identity(request, result)
        if (
            any(getattr(record, key) != value for key, value in identity.items())
            or record.status != result.status.value
            or record.evidence_kind != result.evidence_kind
        ):
            raise ValueError("call record does not match its artifacts")
        if isinstance(result, ToolObservation):
            self.artifacts.read_text(result.artifact)
        if isinstance(request, (WorkspaceToolRequest, ArtifactBrowseRequest, EpisodeToolRequest)) and (
            record.grant_id != request.grant_id
            or record.revision_id != request.revision_id
            or record.execution_epoch != request.execution_epoch
        ):
            raise ValueError("workspace_call_authorization_context_mismatch")
        return record, request, result
