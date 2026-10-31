"""Trusted local registrations. No profile commands are taken from model output."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from domain.context import ContextSettings
from domain.plans import READ_ONLY, PlanScope
from domain.repair_loop import RepairRegistration
from domain.replanning import ReplanSettings
from domain.tasks import RunLimits, Task, TaskLifecycle
from domain.workspace_tools import OPERATIONS, Operation, TaskPackage, TestProfile, WorkspaceGrant
from runtime.call_records import CallRecordStore
from runtime.container_runner import ContainerLimits, ContainerRunner
from runtime.plan_control import PlanController
from runtime.repair_loop import RepairLoop
from runtime.workspace_export import materialize_package
from runtime.workspace_session import WorkspaceSession
from tools.workspace_files import WorkspaceFiles


def registered_profile(package: TaskPackage, name: str):
    writes: tuple[str, ...]
    protected: tuple[str, ...]
    operations: frozenset[Operation] = frozenset(OPERATIONS)
    if name == "fixture-public" and package.task.repo == "fixture/evaluator":
        writes, protected = ("subject.py",), ("public_test.py",)
        profiles = {
            "reproduce": TestProfile(
                profile_id="reproduce",
                argv=(
                    "/usr/local/bin/python",
                    "-c",
                    "from subject import add; print('add(2,3)=',add(2,3)); assert add(2,3)==5",
                ),
            ),
            "public-test": TestProfile(
                profile_id="public-test", argv=("/usr/local/bin/python", "-B", "public_test.py")
            ),
        }
    elif (
        name == "boltons-dictutils" and package.task.repo == "mahmoud/boltons" and package.task.task_id == "boltons-600"
    ):
        writes, protected = (
            ("boltons/dictutils.py",),
            ("tests", "setup.py", "setup.cfg", "tox.ini", "conftest.py", "pyproject.toml"),
        )
        # Public operator-authored reproduction, disclosed to the model. It is
        # separate from evaluator rules and does not write the task package.
        reproduction = "from boltons.dictutils import ManyToMany\nm=ManyToMany()\ntry:\n m.add('k',[])\nexcept TypeError:\n pass\nprint('after rejected add:',len(m),bool(m),'k' in m)\nassert len(m)==0 and not m and 'k' not in m\n"
        profiles = {
            "reproduce": TestProfile(profile_id="reproduce", argv=("/usr/local/bin/python", "-c", reproduction)),
            "public-test": TestProfile(
                profile_id="public-test",
                argv=(
                    "/usr/local/bin/python",
                    "-m",
                    "pytest",
                    "tests/test_dictutils.py",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ),
            ),
        }
    else:
        raise ValueError("unregistered_repair_profile_or_package")
    limits = RunLimits(max_tool_calls=24, max_model_calls=12, max_wall_seconds=600)
    scope = PlanScope(
        read_paths=(".",),
        write_paths=writes,
        operations=operations,
        test_profiles=tuple(profiles),
        limits=limits,
        max_test_seconds=20,
    )
    registration = RepairRegistration(
        scope=scope,
        reproduction_profile=next(iter(profiles)),
        required_profiles=tuple(profiles),
        protected_paths=protected,
    )
    return registration, profiles


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
    loop = RepairLoop(controller, package, registration, replan_settings, context_settings)
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
