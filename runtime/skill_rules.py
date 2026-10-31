"""Conservative visible workflow extraction and parameter binding; no executable source or shell."""

import json

from domain.plans import CandidateLocation, path_subset
from domain.procedural_memory import SkillDocument, SkillParameter, SkillStep
from domain.repair_loop import PlanDraft
from runtime.semantic_rules import applies


def successful_flow(doc, blobs):
    if doc.outcome != "self_test_passed":
        raise ValueError("skill_source_requires_visible_success")
    reads, profiles = [], []
    for event in doc.events:
        if event.kind != "tool" or event.status != "SUCCEEDED" or event.operation not in {"read", "run_test"}:
            continue
        payload = json.loads(blobs[str(event.anchor_id)])
        request = payload["request"]
        args = request["arguments"]
        if (
            args["operation"] == "read"
            and args.get("start_line") is None
            and request["expected_version"] == doc.source_final_version
        ):
            reads.append(args["path"])
        if args["operation"] == "run_test" and request["expected_version"] == doc.source_final_version:
            run = payload["payload"].get("run", {})
            if run.get("exit_code") == 0 and run.get("cleanup_confirmed") and not run.get("reason"):
                profiles.append(args["profile_id"])
    if not reads or not profiles:
        raise ValueError("skill_source_missing_read_and_passed_test")
    return tuple(dict.fromkeys(reads)), tuple(dict.fromkeys(profiles))


def extract(documents, blobs, *, skill_id, revision, name, parent_hash=None):
    if (
        len(documents) < 2
        or len({d.source_task_id for d in documents}) != len(documents)
        or len({d.source_instance_id for d in documents}) != len(documents)
    ):
        raise ValueError("skill_requires_distinct_repeated_successes")
    first = documents[0]
    if any(d.scope != first.scope or d.user_id != first.user_id for d in documents):
        raise ValueError("skill_sources_require_same_declared_scope")
    flows = [successful_flow(doc, blobs[str(doc.episode_id)]) for doc in documents]
    paths = tuple(p for p in flows[0][0] if all(p in f[0] for f in flows))
    profiles = tuple(p for p in flows[0][1] if all(p in f[1] for f in flows))
    if not paths or not profiles or len(profiles) > 6:
        raise ValueError("skill_no_common_bounded_workflow")
    from domain.episodic_memory import EpisodeRef
    from domain.plans import document_hash

    return SkillDocument(
        skill_id=skill_id,
        revision=revision,
        user_id=first.user_id,
        name=name,
        purpose="Read the selected current source and verify already registered public tests in the declared environment.",
        scope=first.scope,
        parameters=(
            SkillParameter(name="source", kind="path", allowed=paths),
            *(
                SkillParameter(name=f"test_{i}", kind="registered_profile", allowed=(profile,))
                for i, profile in enumerate(profiles)
            ),
        ),
        allowed_tools=("read", "run_test"),
        steps=(
            SkillStep(operation="read", parameter="source"),
            *(SkillStep(operation="run_test", parameter=f"test_{i}") for i in range(len(profiles))),
        ),
        preconditions=("files_present", "registered_profiles", "exact_environment", "read_scope"),
        postconditions=("all_tests_passed", "workspace_unchanged"),
        sources=tuple(EpisodeRef(episode_id=d.episode_id, document_hash=document_hash(d)) for d in documents),
        parent_hash=parent_hash,
    )


def bind(document, query, parameters, profiles):
    if not applies(document, query):
        raise ValueError("skill_scope_or_environment_mismatch")
    if set(parameters) != {p.name for p in document.parameters}:
        raise ValueError("skill_exact_parameters_required")
    for parameter in document.parameters:
        value = parameters[parameter.name]
        if value not in parameter.allowed:
            raise ValueError("skill_parameter_outside_validated_values")
        if parameter.kind == "path":
            if query.file_hashes.get(value) is None or not path_subset((value,), query.paths):
                raise ValueError("skill_required_file_or_permission_missing")
        elif value not in profiles:
            raise ValueError("skill_registered_profile_missing")
    actions = tuple(
        {"operation": s.operation, "path" if s.operation == "read" else "profile_id": parameters[s.parameter]}
        for s in document.steps
    )
    draft = PlanDraft(
        understanding=document.purpose,
        candidates=tuple(CandidateLocation(path=parameters[p.name]) for p in document.parameters if p.kind == "path"),
        hypotheses=("This workflow may apply; current code correctness remains unverified.",),
        change_strategy=("Inspect current code. This workflow proposes no edit or dependency installation.",),
        validation_strategy=tuple(
            "Run registered profile " + a["profile_id"] for a in actions if a["operation"] == "run_test"
        ),
    )
    return {
        "draft": draft.model_dump(mode="json"),
        "actions": list(actions),
        "parameters": parameters,
        "workspace_version": query.workspace_version,
        "grants_authority": False,
        "failure_return": document.failure_return,
        "execution_requires_current_authorization": True,
    }
