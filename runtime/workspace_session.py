"""Single-process owner of a task workspace, grants, budgets and completed-call evidence.

This is not a crash-safe intent ledger, distributed lease or automatic recovery.
Those remain P2-10..12. On ambiguous side effects this session stops for inspection.
"""

import json
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import UUID

from domain.models import ToolObservation, ToolStatus
from domain.plans import document_hash
from domain.tasks import SelfTestStatus, Task, TaskLifecycle
from domain.workspace_tools import InvestigateArgs, RunTestArgs, TestProfile, WorkspaceGrant, WorkspaceToolRequest
from domain.workspaces import WorkspaceIdentity, relative_parts
from runtime.call_records import CallRecordStore
from runtime.container_runner import ContainerRunner
from runtime.investigation_records import InvestigationRecords
from runtime.task_state import reserve_tool_call, transition_task
from tools.readonly_workspace import WorkspaceRefused
from tools.workspace_files import WorkspaceFiles, workspace_version


class DispatchPolicy(Protocol):
    def check(self, request: WorkspaceToolRequest) -> None: ...
    def check_budget(self, request: WorkspaceToolRequest) -> None: ...
    def allows_nonrunning(self, request: WorkspaceToolRequest) -> bool: ...
    def effective_grant(self, grant: WorkspaceGrant) -> WorkspaceGrant: ...
    def after_call(self, request: WorkspaceToolRequest, observation: ToolObservation) -> None: ...


class WorkspaceSession:
    def __init__(
        self,
        task: Task,
        identity: WorkspaceIdentity,
        files: WorkspaceFiles,
        grants: Mapping[UUID, WorkspaceGrant],
        profiles: Mapping[str, TestProfile],
        runner: ContainerRunner,
        records: CallRecordStore,
    ):
        if (task.task_id, task.repo, task.base_commit) != (identity.task_id, identity.repo, identity.base_commit):
            raise ValueError("task_workspace_identity_mismatch")
        if task.lifecycle != TaskLifecycle.RUNNING or task.execution_epoch is None or task.grant_id is None:
            raise ValueError("running_authorized_task_required")
        self.task, self.identity, self.files = task, identity, files
        self.grants, self.profiles, self.runner, self.records = grants, profiles, runner, records
        version = workspace_version(files.snapshot())
        self.task = Task.model_validate({**task.model_dump(), "workspace_version": version})
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.cancelled = threading.Event()
        self.blocked = False
        self.seen: set[UUID] = set()
        self.investigations = InvestigationRecords(records.root / "investigations", records.artifacts)
        self.policy: DispatchPolicy | None = None  # Managed product sessions attach a fail-closed plan policy.
        self.durable: Any = None  # Optional P2-10 owner; legacy probes stay explicit single-process sessions.

    def cancel(self):
        self.cancelled.set()
        if self.task.lifecycle in {
            TaskLifecycle.RUNNING,
            TaskLifecycle.CREATED,
            TaskLifecycle.WAITING_INPUT,
            TaskLifecycle.WAITING_APPROVAL,
            TaskLifecycle.PAUSED,
        }:
            self.task = transition_task(self.task, TaskLifecycle.CANCELLED)

    def stop_reason(self, request):
        if self.durable is not None:
            try:
                self.durable.check()
            except ValueError as exc:
                return str(exc)
        if self.cancelled.is_set():
            return "cancelled"
        if self.policy is not None:
            try:
                self.policy.check(request)
            except ValueError as exc:
                return str(exc)
        if self.task.lifecycle != TaskLifecycle.RUNNING and not (
            self.policy and self.policy.allows_nonrunning(request)
        ):
            return "task_not_running"
        grant = self.grants.get(request.grant_id)
        if grant is None or grant.grant_id != request.grant_id:
            return "grant_missing"
        try:
            grant.check(self.identity, request, datetime.now(timezone.utc))
        except ValueError:
            return "authorization_changed"
        if self.task.execution_epoch != request.execution_epoch:
            return "execution_epoch_changed"
        if time.monotonic() - self.started >= self.task.limits.max_wall_seconds:
            return "task_wall_limit"
        return None

    def call(self, request: WorkspaceToolRequest, *, retry_of: UUID | None = None) -> ToolObservation:
        if not self.lock.acquire(blocking=False):
            raise WorkspaceRefused("workspace_call_already_active")
        try:
            if self.durable is not None:
                reason = self.stop_reason(request)
                if reason or request.grant_id != self.task.grant_id:
                    raise WorkspaceRefused(reason or "task_grant_mismatch")
                intent = self.durable.intent(
                    "tool", request.call_id, request.arguments.operation, request.model_dump(mode="json")
                )
                if retry_of is not None:
                    intent = intent.model_copy(update={"retry_of": retry_of})
                prior = self.durable.store.call(self.task.task_id, request.call_id)
                if prior is not None:
                    from domain.execution import CallIntent

                    if prior["intent"]["request"] != request.model_dump(mode="json"):
                        raise WorkspaceRefused("idempotency_key_payload_conflict")
                    intent = CallIntent.model_validate(prior["intent"])
                with self.durable.channel(intent) as previous:
                    if previous is not None:
                        return ToolObservation.model_validate(previous["result"]["observation"])
                    observation = self._call(request)
                    self.durable.finish(
                        intent, observation.status.value, {"observation": observation.model_dump(mode="json")}
                    )
                    return observation
            return self._call(request)
        finally:
            self.lock.release()

    def _call(self, request):
        if self.blocked:
            raise WorkspaceRefused("session_requires_inspection")
        if request.call_id in self.seen or (self.records.root / "calls/tool" / f"{request.call_id}.json").exists():
            raise WorkspaceRefused("duplicate_call_requires_inspection")
        if request.grant_id != self.task.grant_id:
            raise WorkspaceRefused("task_grant_mismatch")
        reason = self.stop_reason(request)
        if reason:
            raise WorkspaceRefused(reason)
        grant = self.grants[request.grant_id]
        if self.policy is not None:
            self.policy.check_budget(request)
            grant = self.policy.effective_grant(grant)
        if request.expected_version != self.task.workspace_version:
            raise WorkspaceRefused("workspace_version_conflict")
        snapshot = self.files.snapshot()
        if workspace_version(snapshot) != request.expected_version:
            self.blocked = True
            raise WorkspaceRefused("external_workspace_change")
        self.task = reserve_tool_call(
            self.task,
            elapsed_seconds=time.monotonic() - self.started,
            readonly_nonrunning=bool(self.policy and self.policy.allows_nonrunning(request)),
        )
        self.seen.add(request.call_id)
        output = {
            "input_version": request.expected_version,
            "source_kind": self.task.source_kind,
            "execution_context": {
                "environment_id": self.task.environment_id,
                "image_id": self.runner.image,
                "container_limits_hash": document_hash(asdict(self.runner.limits)),
                "profile_hash": document_hash(self.profiles[request.arguments.profile_id])
                if isinstance(request.arguments, RunTestArgs) and request.arguments.profile_id in self.profiles
                else None,
            },
        }
        status = ToolStatus.SUCCEEDED
        code: int | None = 0
        try:
            if isinstance(request.arguments, RunTestArgs):
                profile = self.profiles.get(request.arguments.profile_id)
                if profile is None or profile.profile_id != request.arguments.profile_id:
                    raise WorkspaceRefused("test_profile_unavailable")
                run = self.runner.run(
                    self.task.task_id,
                    request.call_id,
                    request.execution_epoch,
                    snapshot,
                    profile,
                    max_seconds=min(
                        grant.max_seconds, self.task.limits.max_wall_seconds - (time.monotonic() - self.started)
                    ),
                    max_output_bytes=grant.max_output_bytes,
                    should_stop=lambda: self.stop_reason(request),
                )
                output["run"] = run
                if run["reason"]:
                    status, code = ToolStatus.FAILED, 124
                elif run["exit_code"] is None or run["exit_code"] in {125, 126, 127}:
                    status, code = ToolStatus.UNKNOWN, None
                elif run["exit_code"] != 0:
                    status, code = ToolStatus.FAILED, run["exit_code"]
                # Only the scratch container can be modified by tests; those changes are discarded.
                output["self_test_status"] = (
                    "PASSED"
                    if status == ToolStatus.SUCCEEDED
                    else "FAILED"
                    if status == ToolStatus.FAILED
                    else "UNKNOWN"
                )
            else:
                # Build indexes only from the authorized view; excluded files must not
                # leak through index diagnostics, counts or symbol candidates.
                visible = {
                    path: text
                    for path, text in snapshot.items()
                    if any(
                        relative_parts(path)[: len(prefix)] == prefix
                        for prefix in (relative_parts(scope, allow_root=True) for scope in grant.paths)
                    )
                }
                arguments = request.arguments
                if isinstance(arguments, InvestigateArgs) and not arguments.issue:
                    arguments = InvestigateArgs.model_validate({**arguments.model_dump(), "issue": self.task.issue})
                    output["issue_source"] = "task_visible_issue"
                output["result"] = self.files.execute(
                    arguments,
                    visible if arguments.operation != "edit" else snapshot,
                    grant.max_output_bytes,
                    version=request.expected_version,
                )
            after = self.files.snapshot()
            next_version = workspace_version(after)
            if not isinstance(request.arguments, RunTestArgs) and request.arguments.operation == "edit":
                expected = dict(snapshot)
                if request.arguments.text is None:
                    previous_text = expected.pop(request.arguments.path)
                    if request.arguments.destination is not None:
                        expected[request.arguments.destination] = previous_text
                else:
                    expected[request.arguments.path] = request.arguments.text
                if after != expected:
                    raise WorkspaceRefused("unexpected_workspace_change")
            elif after != snapshot:
                raise WorkspaceRefused("unexpected_workspace_change")
            late = self.stop_reason(request)
            if late:
                output["late_result_discarded"] = late
                status, code = ToolStatus.FAILED, 125
            self.task = Task.model_validate(
                {
                    **self.task.model_dump(),
                    "workspace_version": next_version,
                    **(
                        {"self_test_status": SelfTestStatus.NOT_RUN} if next_version != request.expected_version else {}
                    ),
                    **(
                        {"self_test_status": SelfTestStatus(output["self_test_status"])}
                        if isinstance(request.arguments, RunTestArgs) and not late
                        else {}
                    ),
                }
            )
            output["output_version"] = next_version
            if not late and request.arguments.operation in {"read", "search", "symbols", "investigate"}:
                evidence = self.records.artifacts.put_text(
                    json.dumps(
                        {
                            "call_id": str(request.call_id),
                            "workspace_version": request.expected_version,
                            "operation": request.arguments.operation,
                            "result": output["result"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                observations = self.investigations.capture(
                    request, output["result"], request.expected_version, visible, evidence
                )
                output["observation_ids"] = [str(observation.observation_id) for observation in observations]
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            # Any edit-side ambiguity or runtime failure requires explicit inspection before reuse.
            reason = str(exc) if isinstance(exc, WorkspaceRefused) else "runtime_operation_failed"
            output["reason"] = reason
            if isinstance(exc, WorkspaceRefused) and reason in {
                "file_precondition_conflict",
                "cannot_delete_missing_file",
                "file_size_limit",
                "workspace_size_limit",
                "case_collision",
                "tool_output_limit",
                "search_match_limit",
                "workspace_file_missing",
                "workspace_directory_missing",
                "test_profile_unavailable",
                "rename_target_must_be_absent",
                "source_line_range_unavailable",
                "source_excerpt_limit",
            }:
                status, code = ToolStatus.FAILED, 1
            else:
                status, code = ToolStatus.UNKNOWN, None
                self.blocked = True
        try:
            observation = ToolObservation(
                task_id=request.task_id,
                call_id=request.call_id,
                tool_name=request.tool_name,
                evidence_kind="real_execution",
                status=status,
                exit_code=code,
                recorded_at=datetime.now(timezone.utc),
                artifact=self.records.artifacts.put_text(json.dumps(output, ensure_ascii=False, indent=2)),
            )
            self.records.save(self.task, request, observation)
            if self.policy is not None:
                self.policy.after_call(request, observation)
        except BaseException:
            self.blocked = True
            raise
        return observation
