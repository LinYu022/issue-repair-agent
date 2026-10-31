"""Single-owner Plan/Execute policy. Proposals never grant their own permissions."""

import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from domain.models import ToolObservation
from domain.plans import (
    READ_ONLY,
    ApprovalDecision,
    Authorization,
    ControlRequest,
    HumanHint,
    PlanProposal,
    PlanRevision,
    RevokeAuthorization,
    document_hash,
    path_subset,
)
from domain.tasks import Task, TaskLifecycle
from domain.workspace_tools import WorkspaceGrant, WorkspaceToolRequest
from runtime.planning_records import PlanningRecords
from runtime.task_state import RunLimitExceeded, reserve_model_call, transition_task
from runtime.workspace_session import WorkspaceSession
from tools.readonly_workspace import WorkspaceRefused
from tools.source_spans import physical_lines
from tools.workspace_files import workspace_version

TERMINAL = {TaskLifecycle.COMPLETED, TaskLifecycle.FAILED, TaskLifecycle.CANCELLED}


class PlanController:
    def __init__(
        self,
        session: WorkspaceSession,
        *,
        provenance: Literal["development_fixture", "local_operator"] = "local_operator",
        reopen_records: bool = False,
    ):
        if session.policy is not None or session.task.lifecycle != TaskLifecycle.RUNNING:
            raise ValueError("fresh_running_session_required")
        if provenance == "development_fixture" and not session.identity.repo.startswith("fixture/"):
            raise ValueError("fixture_approval_provenance_mismatch")
        if session.task.grant_id is None or session.task.execution_epoch is None:
            raise ValueError("bootstrap_grant_and_epoch_required")
        previous = session.grants.get(session.task.grant_id)
        if previous is None or previous.revoked or previous.expires_at <= datetime.now(timezone.utc):
            raise ValueError("live_operator_bootstrap_required")
        self.session, self.provenance = session, provenance
        self.bootstrap = previous
        self.hard_limits = session.task.limits
        self.plan_id = uuid4()
        self.current: PlanRevision | None = None
        self.authorization: Authorization | None = None
        self.binding: UUID | None = None
        self.revoked: set[UUID] = set()
        self.mode = "PLAN"
        self.state_lock = threading.RLock()
        self.records = (PlanningRecords.reopen if reopen_records else PlanningRecords)(
            session.records.root / "planning"
        )
        self.requests: set[UUID] = set()
        self.discovery = WorkspaceGrant(
            identity=session.identity,
            issuer=previous.issuer,
            paths=previous.paths,
            write_paths=(),
            operations=frozenset(previous.operations & READ_ONLY),
            test_profiles=(),
            expires_at=previous.expires_at,
            max_output_bytes=previous.max_output_bytes,
            max_seconds=previous.max_seconds,
        )
        self.registry = {self.discovery.grant_id: self.discovery}
        session.grants = self.registry  # Drop every pre-attached execution capability from this managed session.
        session.policy = self
        session.task = transition_task(session.task, TaskLifecycle.WAITING_APPROVAL)
        self._task(
            grant_id=self.discovery.grant_id, revision_id=None, execution_epoch=(session.task.execution_epoch or 0) + 1
        )
        self._save()

    def _task(self, **changes):
        self.session.task = Task.model_validate({**self.session.task.model_dump(), **changes})

    def _state(self, target: TaskLifecycle):
        if self.session.task.lifecycle != target:
            self.session.task = transition_task(self.session.task, target)

    def _live(self):
        if self.session.durable is not None:
            self.session.durable.check()
        if self.session.blocked or self.session.task.lifecycle in TERMINAL:
            raise WorkspaceRefused("planning_task_stopped_or_requires_inspection")

    def _request(self, request_id: UUID):
        if request_id in self.requests or (self.records.root / "events" / f"{request_id}.json").exists():
            raise WorkspaceRefused("control_request_already_recorded")

    def _workspace(self, expected: str):
        if expected != self.session.task.workspace_version:
            raise WorkspaceRefused("control_workspace_version_conflict")
        if workspace_version(self.session.files.snapshot()) != expected:
            self.session.blocked = True
            raise WorkspaceRefused("external_workspace_change_requires_inspection")

    def _save(self):
        try:
            if self.session.durable is not None:
                self.session.durable.persist(self.view())
            self.records.snapshot(self.view())
        except BaseException:
            self.session.blocked = True
            raise

    def _event(self, request_id, kind, role, payload):
        try:
            self.records.event(request_id, kind, role, payload)
            self.requests.add(request_id)
        except BaseException:
            self.session.blocked = True
            raise

    def view(self):
        with self.state_lock:
            return {
                "mode": self.mode,
                "task": self.session.task.model_dump(mode="json"),
                "plan": None if self.current is None else self.current.model_dump(mode="json"),
                "revision_hash": None if self.current is None else document_hash(self.current),
                "authorization": None if self.authorization is None else self.authorization.model_dump(mode="json"),
                "authorization_active": self._authorization_valid(),
                "bound_authorization_id": None if self.binding is None else str(self.binding),
                "planning_event_count": len(self.records.events),
                "human_hint_count": sum(e["kind"] == "human_diagnostic_hint" for e in self.records.events),
                "authority_restored_after_restart": False,
            }

    def _authorization_valid(self):
        auth = self.authorization
        if auth is None or auth.authorization_id in self.revoked:
            return False
        return (
            datetime.now(timezone.utc) < auth.grant.expires_at
            and self.registry.get(auth.authorization_id) == auth.grant
            and auth.environment_id == self.session.task.environment_id
            and auth.grant.identity == self.session.identity
            and auth.image_id == self.session.runner.image
            and auth.container_limits_hash == document_hash(asdict(self.session.runner.limits))
            and all(
                name in self.session.profiles and document_hash(self.session.profiles[name]) == value
                for name, value in auth.profile_hashes
            )
        )

    def _same_target(self, revision: PlanRevision):
        return (revision.body.repo, revision.body.base_commit) == (
            self.session.identity.repo,
            self.session.identity.base_commit,
        )

    def _environment_matches(self, revision):
        return (
            revision.environment_id == self.session.task.environment_id
            and revision.image_id == self.session.runner.image
            and revision.container_limits_hash == document_hash(asdict(self.session.runner.limits))
            and all(
                name in self.session.profiles and document_hash(self.session.profiles[name]) == value
                for name, value in revision.profile_hashes
            )
        )

    def _validate_proposal(self, proposal: PlanProposal):
        if proposal.expected_revision_id != (None if self.current is None else self.current.revision_id):
            raise WorkspaceRefused("plan_revision_conflict")
        self._workspace(proposal.expected_workspace_version)
        scope = proposal.body.scope
        if not path_subset(scope.read_paths, self.discovery.paths):
            raise WorkspaceRefused("requested_read_scope_requires_operator_workspace_registration")
        if (
            scope.max_output_bytes > self.bootstrap.max_output_bytes
            or scope.max_test_seconds > self.bootstrap.max_seconds
            or any(
                getattr(scope.limits, key) > getattr(self.hard_limits, key)
                for key in ("max_tool_calls", "max_model_calls", "max_wall_seconds")
            )
        ):
            raise WorkspaceRefused("requested_limits_exceed_registered_hard_limits")
        if (
            scope.limits.max_tool_calls < self.session.task.tool_calls_reserved
            or scope.limits.max_model_calls < self.session.task.model_calls_reserved
        ):
            raise WorkspaceRefused("requested_limits_below_already_reserved_usage")
        if any(name not in self.session.profiles for name in scope.test_profiles):
            raise WorkspaceRefused("test_profile_not_operator_registered")
        for candidate in proposal.body.candidates:
            if not path_subset((candidate.path,), scope.read_paths):
                raise WorkspaceRefused("plan_candidate_outside_requested_scope")
        for call_id in proposal.body.evidence_call_ids:
            record, request, result = self.session.records.read(
                self.session.records.root / "calls/tool" / f"{call_id}.json"
            )
            if (
                not isinstance(request, WorkspaceToolRequest)
                or not isinstance(result, ToolObservation)
                or record.task_id != self.session.task.task_id
                or request.workspace_id != self.session.identity.workspace_id
                or record.workspace_version != proposal.expected_workspace_version
            ):
                raise WorkspaceRefused("plan_evidence_not_current_task_workspace")
        if (proposal.body.repo, proposal.body.base_commit) == (
            self.session.identity.repo,
            self.session.identity.base_commit,
        ):
            files = self.session.files.snapshot()
            for candidate in proposal.body.candidates:
                if candidate.line is not None and (
                    candidate.path not in files or candidate.line > len(physical_lines(files[candidate.path]))
                ):
                    raise WorkspaceRefused("plan_candidate_line_not_in_current_source")

    def propose(self, proposal: PlanProposal):
        if not self.session.lock.acquire(blocking=False):
            raise WorkspaceRefused("cannot_revise_while_tool_active")
        try:
            with self.state_lock:
                self._live()
                self._request(proposal.request_id)
                self._validate_proposal(proposal)
                reuse = bool(
                    self.authorization
                    and self._authorization_valid()
                    and (proposal.body.repo, proposal.body.base_commit)
                    == (self.session.identity.repo, self.session.identity.base_commit)
                    and proposal.body.scope.contained_by(self.authorization.scope)
                )
                revision = PlanRevision(
                    plan_id=self.plan_id,
                    task_id=self.session.task.task_id,
                    workspace_id=self.session.identity.workspace_id,
                    parent_revision_id=proposal.expected_revision_id,
                    workspace_version=proposal.expected_workspace_version,
                    reason=proposal.reason,
                    body=proposal.body,
                    created_at=datetime.now(timezone.utc),
                    linked_authorization_id=self.authorization.authorization_id
                    if reuse and self.authorization
                    else None,
                    environment_id=self.session.task.environment_id,
                    image_id=self.session.runner.image,
                    profile_hashes=tuple(
                        (name, document_hash(self.session.profiles[name])) for name in proposal.body.scope.test_profiles
                    ),
                    container_limits_hash=document_hash(asdict(self.session.runner.limits)),
                )
                self.records.save("revisions", revision)
                self._event(
                    proposal.request_id,
                    "plan_revised_within_authorization" if reuse else "authorization_requested",
                    "planner",
                    revision,
                )
                self.current = revision
                self.binding = revision.linked_authorization_id
                self.mode = "EXECUTE" if reuse else "PLAN"
                self._task(
                    revision_id=revision.revision_id,
                    grant_id=self.binding or self.discovery.grant_id,
                    limits=revision.body.scope.limits if reuse else self.hard_limits,
                )
                self._state(
                    TaskLifecycle.RUNNING
                    if reuse
                    else TaskLifecycle.WAITING_APPROVAL
                    if self._same_target(revision)
                    else TaskLifecycle.WAITING_INPUT
                )
                self._save()
                return self.view()
        finally:
            self.session.lock.release()

    def decide(self, decision: ApprovalDecision):
        if not self.session.lock.acquire(blocking=False):
            raise WorkspaceRefused("cannot_approve_while_tool_active")
        try:
            with self.state_lock:
                self._live()
                self._request(decision.request_id)
                revision = self.current
                if (
                    revision is None
                    or revision.revision_id != decision.revision_id
                    or document_hash(revision) != decision.revision_hash
                ):
                    raise WorkspaceRefused("approval_not_for_current_reviewed_revision")
                self._workspace(decision.expected_workspace_version)
                if revision.workspace_version != decision.expected_workspace_version:
                    raise WorkspaceRefused("approval_requires_current_planning_snapshot")
                if decision.decision == "approve" and not self._same_target(revision):
                    raise WorkspaceRefused("new_repository_or_base_requires_new_workspace_and_authorization")
                if decision.decision == "approve" and not self._environment_matches(revision):
                    raise WorkspaceRefused("reviewed_environment_changed_requires_plan_revision")
                if (
                    decision.decision == "approve"
                    and not datetime.now(timezone.utc) < decision.expires_at <= self.discovery.expires_at
                ):
                    raise WorkspaceRefused("approval_expiry_outside_registered_window")
                if decision.decision == "deny":
                    self._event(decision.request_id, "execution_denied", "approver", decision)
                    if self.authorization and self.binding == self.authorization.authorization_id:
                        self.revoked.add(self.authorization.authorization_id)
                    self.mode = "PLAN"
                    self.binding = None
                    self._task(grant_id=self.discovery.grant_id, limits=self.hard_limits)
                    self._state(TaskLifecycle.WAITING_APPROVAL)
                else:
                    scope = revision.body.scope
                    if self.session.task.tool_calls_reserved >= scope.limits.max_tool_calls:
                        raise WorkspaceRefused("approved_tool_budget_already_exhausted")
                    if (
                        self.session.task.model_calls_reserved >= scope.limits.max_model_calls
                        or time.monotonic() - self.session.started >= scope.limits.max_wall_seconds
                    ):
                        raise WorkspaceRefused("approved_model_or_wall_budget_already_exhausted")
                    grant = WorkspaceGrant(
                        identity=self.session.identity,
                        issuer=self.bootstrap.issuer,
                        paths=scope.read_paths,
                        write_paths=scope.write_paths,
                        operations=scope.operations,
                        test_profiles=scope.test_profiles,
                        expires_at=decision.expires_at,
                        max_output_bytes=scope.max_output_bytes,
                        max_seconds=scope.max_test_seconds,
                    )
                    authorization = Authorization(
                        authorization_id=grant.grant_id,
                        revision_id_at_approval=revision.revision_id,
                        revision_hash_at_approval=document_hash(revision),
                        grant=grant,
                        scope=scope,
                        image_id=self.session.runner.image,
                        environment_id=self.session.task.environment_id,
                        profile_hashes=tuple(
                            (name, document_hash(self.session.profiles[name])) for name in scope.test_profiles
                        ),
                        container_limits_hash=document_hash(asdict(self.session.runner.limits)),
                        approved_at=datetime.now(timezone.utc),
                        provenance=self.provenance,
                    )
                    self.records.save("authorizations", authorization)
                    self._event(
                        decision.request_id,
                        "execution_approved",
                        "approver",
                        {
                            "decision": decision.model_dump(mode="json"),
                            "authorization": authorization.model_dump(mode="json"),
                        },
                    )
                    # A new approval rotates both grant and execution generation. Old requests cannot resume.
                    if self.authorization:
                        self.revoked.add(self.authorization.authorization_id)
                    self.authorization = authorization
                    self.registry[grant.grant_id] = grant
                    self.binding = grant.grant_id
                    self.mode = "EXECUTE"
                    self._task(
                        grant_id=grant.grant_id,
                        execution_epoch=(self.session.task.execution_epoch or 0) + 1,
                        limits=scope.limits,
                    )
                    self._state(TaskLifecycle.RUNNING)
                self._save()
                return self.view()
        finally:
            self.session.lock.release()

    def revoke(self, request: RevokeAuthorization):
        # Revocation must be deliverable while a test owns the workspace call lock.
        with self.state_lock:
            self._live()
            self._request(request.request_id)
            if self.authorization is None or request.authorization_id != self.authorization.authorization_id:
                raise WorkspaceRefused("authorization_not_current")
            self._event(request.request_id, "authorization_revoked", "approver", request)
            self.revoked.add(request.authorization_id)
            self.registry[request.authorization_id] = self.authorization.grant.model_copy(update={"revoked": True})
            self.mode = "PLAN"
            self.binding = None
            self._task(grant_id=self.discovery.grant_id, limits=self.hard_limits)
            self._state(TaskLifecycle.WAITING_APPROVAL)
            self._save()
            return self.view()

    def hint(self, request: HumanHint):
        with self.state_lock:
            self._live()
            self._request(request.request_id)
            self._event(request.request_id, "human_diagnostic_hint", "approver", request)
            self._save()
            return {"recorded": True, "kind": "human_diagnostic_hint", "permissions_changed": False}

    def control(self, request: ControlRequest):
        if request.action not in {"cancel", "pause"} and not self.session.lock.acquire(blocking=False):
            raise WorkspaceRefused("cannot_change_mode_while_tool_active")
        try:
            with self.state_lock:
                self._live()
                self._request(request.request_id)
                if request.expected_revision_id != (None if self.current is None else self.current.revision_id):
                    raise WorkspaceRefused("control_revision_conflict")
                action = request.action
                if action == "resume":
                    if (
                        self.current is None
                        or not self._authorization_valid()
                        or self.authorization is None
                        or not self.current.body.scope.contained_by(self.authorization.scope)
                        or not self._same_target(self.current)
                        or not self._environment_matches(self.current)
                    ):
                        raise WorkspaceRefused("resume_requires_current_authorization")
                    self.mode = "EXECUTE"
                    self.binding = self.authorization.authorization_id
                    self._task(grant_id=self.binding, limits=self.current.body.scope.limits)
                    self._state(TaskLifecycle.RUNNING)
                elif action == "cancel":
                    self.session.cancel()
                elif action == "pause":
                    self._state(TaskLifecycle.PAUSED)
                elif action in {"plan", "wait_input"}:
                    self.mode = "PLAN"
                    self.binding = None
                    self._task(grant_id=self.discovery.grant_id, limits=self.hard_limits)
                    self._state(
                        TaskLifecycle.WAITING_INPUT if action == "wait_input" else TaskLifecycle.WAITING_APPROVAL
                    )
                else:
                    if self.mode != "EXECUTE":
                        raise WorkspaceRefused("finish_requires_execution_mode")
                    self._state(TaskLifecycle.COMPLETED if action == "complete" else TaskLifecycle.FAILED)
                self._event(request.request_id, "lifecycle_" + action, "planner", request)
                self._save()
                return self.view()
        finally:
            if request.action not in {"cancel", "pause"}:
                self.session.lock.release()

    def allows_nonrunning(self, request):
        return (
            self.mode == "PLAN"
            and request.arguments.operation in READ_ONLY
            and self.session.task.lifecycle not in TERMINAL
        )

    def check(self, request: WorkspaceToolRequest):
        with self.state_lock:
            self._live()
            if request.revision_id != (None if self.current is None else self.current.revision_id):
                raise WorkspaceRefused("tool_plan_revision_conflict")
            if self.mode == "PLAN":
                if request.arguments.operation not in READ_ONLY or request.grant_id != self.discovery.grant_id:
                    raise WorkspaceRefused("plan_mode_readonly")
                return
            if (
                not self.current
                or not self.authorization
                or self.binding != self.authorization.authorization_id
                or not self._authorization_valid()
                or not self._same_target(self.current)
                or not self._environment_matches(self.current)
                or not self.current.body.scope.contained_by(self.authorization.scope)
            ):
                raise WorkspaceRefused("execution_authorization_inactive")
            scope = self.current.body.scope
            if request.arguments.operation not in scope.operations:
                raise WorkspaceRefused("operation_outside_current_plan")
            paths = scope.write_paths if request.arguments.operation == "edit" else scope.read_paths
            if not path_subset((getattr(request.arguments, "path", "."),), paths):
                raise WorkspaceRefused("path_outside_current_plan")
            destination = getattr(request.arguments, "destination", None)
            if destination is not None and not path_subset((destination,), paths):
                raise WorkspaceRefused("rename_destination_outside_current_plan")
            if request.arguments.operation == "run_test" and request.arguments.profile_id not in scope.test_profiles:
                raise WorkspaceRefused("test_outside_current_plan")
            if time.monotonic() - self.session.started >= scope.limits.max_wall_seconds:
                raise WorkspaceRefused("authorization_wall_limit")

    def check_budget(self, request):
        if (
            self.mode == "EXECUTE"
            and self.current
            and self.session.task.tool_calls_reserved >= self.current.body.scope.limits.max_tool_calls
        ):
            raise RunLimitExceeded("authorization_tool_limit")

    def after_call(self, request, observation):
        with self.state_lock:
            self._save()

    def reserve_model(self, *, expected_stamp=None, stamp_provider=None):
        """Trusted synchronous planner entry; reservation survives failed/unknown replies."""
        with self.session.lock, self.state_lock:
            self._live()
            if expected_stamp is not None and (stamp_provider is None or stamp_provider() != expected_stamp):
                raise WorkspaceRefused("model_context_snapshot_changed_before_dispatch")
            planning = self.mode == "PLAN" and self.session.task.lifecycle == TaskLifecycle.WAITING_APPROVAL
            if not planning and (
                self.mode != "EXECUTE"
                or self.session.task.lifecycle != TaskLifecycle.RUNNING
                or self.current is None
                or self.authorization is None
                or self.binding != self.authorization.authorization_id
                or not self._authorization_valid()
                or not self._same_target(self.current)
                or not self._environment_matches(self.current)
                or not self.current.body.scope.contained_by(self.authorization.scope)
            ):
                raise WorkspaceRefused("model_requires_active_planning_or_execution")
            if planning and datetime.now(timezone.utc) >= self.discovery.expires_at:
                raise WorkspaceRefused("planning_window_expired")
            self.session.task = reserve_model_call(
                self.session.task,
                elapsed_seconds=time.monotonic() - self.session.started,
                planning_nonrunning=planning,
            )
            self._save()
            return self.session.task

    def effective_grant(self, grant):
        if self.mode == "EXECUTE" and self.current:
            scope = self.current.body.scope
            return grant.model_copy(
                update={
                    "paths": scope.read_paths,
                    "write_paths": scope.write_paths,
                    "max_output_bytes": min(grant.max_output_bytes, scope.max_output_bytes),
                    "max_seconds": min(grant.max_seconds, scope.max_test_seconds),
                }
            )
        return grant
