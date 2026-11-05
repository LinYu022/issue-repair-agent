"""Trusted local registrations. No profile commands are taken from model output."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from domain.context import ContextSettings
from domain.plans import READ_ONLY
from domain.repair_loop import RepairRegistration
from domain.replanning import ReplanSettings
from domain.tasks import Task, TaskLifecycle
from domain.workspace_tools import TaskPackage, TestProfile, WorkspaceGrant
from runtime.call_records import CallRecordStore
from runtime.container_runner import ContainerLimits, ContainerRunner
from runtime.plan_control import PlanController
from runtime.repair_loop import RepairLoop
from runtime.workspace_export import materialize_package
from runtime.workspace_session import WorkspaceSession
from tools.workspace_files import WorkspaceFiles




def make_repair_loop(
    parent: Path,
    trace: Path,
    package: TaskPackage,
    registration: RepairRegistration,
    profiles: dict[str, TestProfile],
    image: str,
    *,
    provenance="development_fixture",
    replan_settings: ReplanSettings | None = None,
    context_settings: ContextSettings | None = None,
    durable_store=None,
    lock_root: Path | None = None,
    lease_seconds: int = 15,
    container_limits: ContainerLimits = ContainerLimits(),
    run_log=None,
):
    task_id = uuid4()
    path, identity = materialize_package(parent, package, task_id)
    bootstrap = WorkspaceGrant(
        identity=identity,
        issuer="development_fixture" if package.task.source_kind == "contract_fixture" else "operator",
        paths=registration.scope.read_paths,
        operations=READ_ONLY,
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=900),
        max_seconds=registration.scope.max_test_seconds,
        max_output_bytes=registration.scope.max_output_bytes,
    )
    task = Task(
        task_id=task_id,
        source_task_id=package.task.task_id,
        source_kind=package.task.source_kind,
        repo=package.task.repo,
        base_commit=package.task.base_commit,
        issue=package.task.issue,
        environment_id=package.task.environment_id,
        available_at=package.task.available_at,
        lifecycle=TaskLifecycle.RUNNING,
        limits=registration.scope.limits,
        grant_id=bootstrap.grant_id,
        execution_epoch=1,
    )
    session = WorkspaceSession(
        task,
        identity,
        WorkspaceFiles(path),
        {bootstrap.grant_id: bootstrap},
        profiles,
        ContainerRunner(image, trace / "processes", container_limits),
        CallRecordStore(trace),
    )
    controller = PlanController(session, provenance=provenance)
    loop = RepairLoop(controller, package, registration, replan_settings, context_settings, run_log=run_log)
    loop.messages[1]["content"] = json.dumps(
        {
            **json.loads(loop.messages[1]["content"]),
            "public_profile_configuration": {name: p.model_dump(mode="json") for name, p in profiles.items()},
        },
        ensure_ascii=False,
    )
    if durable_store is not None:
        from runtime.execution_guard import DurableExecution

        owner = DurableExecution(
            durable_store,
            task_id,
            path,
            lock_root or trace.parent / "execution-locks",
            session.runner,
            register_task=session.task,
            authority=controller.view(),
            seconds=lease_seconds,
        )
        owner.attach(loop)
    return loop
