"""Episode navigation uses the same persistent tool channel and cumulative budget."""

import json
import time
from datetime import datetime, timezone
from uuid import uuid4

from domain.episodic_memory import EpisodeToolRequest
from domain.models import ToolObservation, ToolStatus
from runtime.episode_provider import EpisodicMemoryProvider
from runtime.task_state import reserve_tool_call
from tools.readonly_workspace import WorkspaceRefused


def episode_tool(loop, action):
    session = loop.session
    provider = loop.context_manager.memory
    if not isinstance(provider, EpisodicMemoryProvider) or session.durable is None:
        raise WorkspaceRefused("episode_tools_require_trusted_persistent_binding")
    with session.lock:
        sentinel = loop._request({"operation": "diff", "path": "."})
        reason = session.stop_reason(sentinel)
        if reason:
            raise WorkspaceRefused(reason)
        loop.controller.check_budget(sentinel)
        request = EpisodeToolRequest(
            task_id=sentinel.task_id,
            workspace_id=sentinel.workspace_id,
            grant_id=sentinel.grant_id,
            revision_id=sentinel.revision_id,
            execution_epoch=sentinel.execution_epoch,
            expected_version=sentinel.expected_version,
            arguments=action,
            call_id=uuid4(),
        )
        intent = session.durable.intent("tool", request.call_id, "episode_memory", request.model_dump(mode="json"))
        with session.durable.channel(intent) as previous:
            if previous is not None:
                return previous["result"]
            session.task = reserve_tool_call(
                session.task,
                elapsed_seconds=time.monotonic() - session.started,
                readonly_nonrunning=loop.controller.allows_nonrunning(sentinel),
            )
            try:
                generation = provider.generation()
                value = provider.navigate(action, record=False)
                if generation != provider.generation() or session.stop_reason(sentinel):
                    raise WorkspaceRefused("episode_state_changed_during_navigation")
                limit = loop.controller.effective_grant(session.grants[sentinel.grant_id]).max_output_bytes
                if len(json.dumps(value, ensure_ascii=False).encode()) > limit:
                    raise WorkspaceRefused("episode_output_limit")
                value = provider.navigate(action, record=True)
                status = ToolStatus.SUCCEEDED
                output = {
                    "result": value,
                    "input_version": request.expected_version,
                    "history_is_not_current_code": True,
                }
            except ValueError as exc:
                status = ToolStatus.FAILED
                output = {
                    "reason": str(exc),
                    "input_version": request.expected_version,
                    "history_is_not_current_code": True,
                }
            observation = ToolObservation(
                task_id=request.task_id,
                call_id=request.call_id,
                tool_name=request.tool_name,
                evidence_kind="real_execution",
                status=status,
                exit_code=0 if status == ToolStatus.SUCCEEDED else 1,
                recorded_at=datetime.now(timezone.utc),
                artifact=session.records.artifacts.put_text(json.dumps(output, ensure_ascii=False)),
            )
            session.records.save(session.task, request, observation)
            loop.controller.after_call(request, observation)
            result = {"status": status.value, "call_id": str(request.call_id), "evidence": output}
            session.durable.finish(intent, status.value, result)
            loop._event(
                "tool_observation",
                call_id=str(request.call_id),
                operation="episode_memory",
                input_version=request.expected_version,
                output_version=session.task.workspace_version,
                status=status.value,
                artifact=observation.artifact.model_dump(mode="json"),
            )
            return result
