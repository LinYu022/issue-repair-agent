"""Rehydrate the existing loop at a decision boundary, with a new exact plan and grant."""

import json
import time
from datetime import datetime, timezone
from uuid import UUID

from domain.context import ArtifactSource, ContextBuildRecord, ContextSettings, TranscriptEntry
from domain.failure_diagnosis import Classification, DeclaredExpectation, DiagnosticEvidence, HypothesisRevision
from domain.models import ToolObservation
from domain.plans import READ_ONLY, ApprovalDecision
from domain.repair_loop import RepairRegistration
from domain.replanning import PolicyDecision, ReplanSettings
from domain.tasks import Task
from domain.workspace_tools import TaskPackage, TestProfile, WorkspaceGrant, WorkspaceToolRequest
from runtime.call_records import CallRecordStore
from runtime.container_runner import ContainerLimits, ContainerRunner
from runtime.plan_control import PlanController
from runtime.public_reproducer import outcome as public_probe_outcome
from runtime.repair_loop import RepairLoop
from runtime.workspace_session import WorkspaceSession
from tools.workspace_files import WorkspaceFiles


def restore_graph(spec, graph, row, review, decision, workspace, trace):
    from runtime.recovery import identity_from

    identity = identity_from(spec)
    registration = RepairRegistration.model_validate(spec["registration"])
    package = TaskPackage.model_validate(spec["package"])
    bootstrap = WorkspaceGrant(
        identity=identity,
        issuer="development_fixture" if spec["provenance"] == "development_fixture" else "operator",
        paths=registration.scope.read_paths,
        operations=READ_ONLY,
        expires_at=decision.expires_at,
        max_seconds=registration.scope.max_test_seconds,
        max_output_bytes=registration.scope.max_output_bytes,
    )
    task = Task.model_validate(
        {
            **row["payload"],
            "lifecycle": "RUNNING",
            "self_test_status": "NOT_RUN",
            "grant_id": bootstrap.grant_id,
            "revision_id": None,
        }
    )
    files = WorkspaceFiles(workspace)
    # Export patches against the original package, never against the recovery point.
    files.baseline = {entry.path: entry.text for entry in package.files}
    profiles = {k: TestProfile.model_validate(v) for k, v in spec["environment"]["profiles"].items()}
    runner = ContainerRunner(
        spec["environment"]["image_id"], trace / "processes", ContainerLimits(**spec["environment"]["resources"])
    )
    session = WorkspaceSession(
        task, identity, files, {bootstrap.grant_id: bootstrap}, profiles, runner, CallRecordStore(trace)
    )
    controller = PlanController(session, provenance=spec["provenance"], reopen_records=True)
    loop = RepairLoop(
        controller,
        package,
        registration,
        ReplanSettings.model_validate(graph.replanning["settings"]),
        ContextSettings.model_validate(graph.context["settings"]).model_copy(update={"memory_enabled": False}),
        reopen_records=True,
    )
    session.started = time.monotonic() - max(
        graph.elapsed_wall_seconds,
        task.limits.max_wall_seconds - (row["deadline"] - datetime.now(timezone.utc)).total_seconds(),
    )
    for key in ("initial_version", "messages", "events", "preflight_done"):
        setattr(loop, key, graph.loop[key])
    loop.baseline_failure = UUID(graph.loop["baseline_failure"]) if graph.loop["baseline_failure"] else None
    for resolution in review.resolutions:
        if resolution["outcome"] != "recorded_result":
            continue
        path = trace / "calls/tool" / (resolution["call_id"] + ".json")
        if not path.exists():
            continue
        record, request, observation = session.records.read(path)
        if (
            isinstance(request, WorkspaceToolRequest)
            and isinstance(observation, ToolObservation)
            and request.arguments.operation == "run_test"
        ):
            payload = json.loads(session.records.artifacts.read_text(observation.artifact))
            run = payload.get("run", {})
            if (
                request.arguments.profile_id == registration.reproduction_profile
                and request.expected_version == loop.initial_version
                and record.status == "FAILED"
                and run.get("exit_code") not in (None, 0, 125, 126, 127)
                and not run.get("reason")
                and not payload.get("late_result_discarded")
                and public_probe_outcome(registration.public_reproducer, run) in {None, "assertion_failed"}
            ):
                loop.baseline_failure = request.call_id
    if loop.baseline_failure is not None:
        loop.preflight_done = True
    loop.reads, loop.tests = {}, {}  # Historical reads/tests cannot justify a new edit/completion.
    policy = loop.replanner
    for key in ("retries", "refusals", "corrections", "decisions", "revisions", "investigated", "require_readonly"):
        setattr(policy, key, graph.replanning[key])
    policy.facts = [DiagnosticEvidence.model_validate(v) for v in graph.replanning["facts"]]
    policy.expectations = {
        k: DeclaredExpectation.model_validate(v) for k, v in graph.replanning["expectations"].items()
    }
    for key, cls in (
        ("hypothesis", HypothesisRevision),
        ("last_classification", Classification),
        ("last_evidence", DiagnosticEvidence),
        ("trigger_evidence", DiagnosticEvidence),
        ("decision", PolicyDecision),
    ):
        value = graph.replanning[key]
        setattr(policy, key, None if value is None else cls.model_validate(value))
    repeat = graph.replanning["repeat_detector"]
    policy.detector.seen = {k: tuple(v) for k, v in repeat["seen"].items()}
    policy.detector.distinct_observations = set(repeat["distinct_observations"])
    policy.detector.evidence_epoch = repeat["evidence_epoch"]
    policy.pin = None
    policy.pending.clear()  # Old policy actions are audit only, never implicitly replayed.
    policy.needs_plan = False
    policy.initial_baselines_scheduled = False
    context = loop.context_manager
    context.transcript = [TranscriptEntry.model_validate(v) for v in graph.context["transcript"]]
    context.builds = [ContextBuildRecord.model_validate(v) for v in graph.context["builds"]]
    for key in ("events", "build_attempts", "pages"):
        setattr(context, key, graph.context[key])
    context.initial_read_scope = tuple(graph.context["initial_read_scope"])
    context.observations = [
        {
            **v,
            "source": ArtifactSource.model_validate(v["source"]),
            "request": WorkspaceToolRequest.model_validate(v["request"]),
            "observation": ToolObservation.model_validate(v["observation"]),
            "code": None if v["code"] is None else ArtifactSource.model_validate(v["code"]),
            "related": tuple(UUID(x) for x in v["related"]),
        }
        for v in graph.context["observations"]
    ]
    controller.current = review.new_plan
    controller.plan_id = review.new_plan.plan_id
    controller.records.save("revisions", review.new_plan)
    controller._task(revision_id=review.new_plan.revision_id)
    controller._event(review.request.recovery_id, "authorization_requested", "planner", review.new_plan)
    controller.decide(
        ApprovalDecision(
            revision_id=review.new_plan.revision_id,
            revision_hash=decision.plan_hash,
            expected_workspace_version=review.new_plan.workspace_version,
            decision="approve",
            expires_at=decision.expires_at,
            reason=decision.reason,
        )
    )
    loop.recovery_info = {
        "enabled": True,
        "restored": True,
        "recovery_id": str(review.request.recovery_id),
        "checkpoint_id": str(review.checkpoint_id),
        "original_authority_reused": False,
        "tail_resolutions": list(review.resolutions),
        "reads_invalidated": True,
        "tests_invalidated": True,
    }
    loop._feedback(
        {
            "recovery": loop.recovery_info,
            "next_action": "Read current files and validate required profiles. Cached model actions were not executed.",
        }
    )
    (trace / "repair" / "recovery.json").write_text(json.dumps(loop.recovery_info, indent=2) + "\n")
    return loop
