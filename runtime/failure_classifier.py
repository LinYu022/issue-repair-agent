"""Conservative multi-label interpretation of versioned, already verified facts.

No raw log keyword determines a label. Model claims are attributed proposals;
these rules do not install dependencies, retry tools, edit files or replan.
"""

from typing import Literal

from domain.failure_diagnosis import (
    Classification,
    DeclaredExpectation,
    DiagnosticEvidence,
    ModelAnalysis,
)
from domain.failure_diagnosis import (
    FailureCategory as C,
)
from runtime.diagnostic_evidence import environment_hash


def classify_failure(
    focus: DiagnosticEvidence,
    history: tuple[DiagnosticEvidence, ...],
    *,
    analyses: tuple[ModelAnalysis, ...] = (),
    expectations: tuple[DeclaredExpectation, ...] = (),
) -> Classification:
    facts = {e.evidence_id: e for e in (*history, focus)}
    if any((e.task_id, e.workspace_id) != (focus.task_id, focus.workspace_id) for e in facts.values()):
        raise ValueError("classification_cannot_mix_task_workspaces")
    if any(a.task_id != focus.task_id for a in analyses):
        raise ValueError("classification_model_analysis_task_mismatch")
    labels: set[C] = set()
    reasons, used = [], {focus.evidence_id}
    confidence: Literal["low", "medium", "high"] = "medium"
    next_check = "Keep code, environment and test profile fixed; obtain a missing independent observation before changing the diagnosis."

    def add(label, reason, other=None):
        labels.add(label)
        reasons.append(reason)
        if other is not None:
            used.add(other.evidence_id)

    def passed(e):
        return e.status == "SUCCEEDED" and e.exit_code == 0 and not e.runner_reason and not e.late_result

    def failed(e):
        return (
            e.status == "FAILED"
            and e.exit_code not in (None, 0, 125, 126, 127, 137)
            and not e.runner_reason
            and not e.late_result
        )

    def comparable(e):
        return bool(
            focus.context
            and e.context
            and e.metadata_state not in {"invalid", "legacy_context_missing"}
            and focus.profile
            and e.profile
            and focus.profile.logical_check_id == e.profile.logical_check_id
            and focus.context.profile_hash == e.context.profile_hash
            and focus.context.container_limits_hash == e.context.container_limits_hash
        )

    if not focus.context or focus.metadata_state in {"invalid", "legacy_context_missing"} or focus.late_result:
        add(
            C.UNCERTAIN,
            "Runtime context/metadata is incomplete, incoherent or late; do not infer a cause from the printed exception.",
        )
    else:
        for previous in sorted(history, key=lambda e: (e.recorded_at, str(e.evidence_id))):
            if previous.recorded_at > focus.recorded_at or not comparable(previous) or previous.late_result:
                continue
            same_code = previous.workspace_version == focus.workspace_version
            same_env = environment_hash(previous.context) == environment_hash(focus.context)
            if same_env and not same_code and passed(previous) and failed(focus):
                add(
                    C.IMPLEMENTATION,
                    "Same environment/profile passed before the code changed and failed afterward; the edit is implicated, not a proven root cause.",
                    previous,
                )
                next_check = "On a fresh copy, restore the pre-edit code while keeping image, dependencies, profile and input fixed; compare the outcome."
                if focus.profile and focus.profile.role == "regression":
                    add(
                        C.REGRESSION,
                        "A declared regression check changed from pass to fail under a code-only contrast.",
                        previous,
                    )
            earlier_failure = (
                previous
                if failed(previous) and passed(focus)
                else focus
                if failed(focus) and passed(previous)
                else None
            )
            if same_code and not same_env and earlier_failure and earlier_failure.metadata and earlier_failure.profile:
                module = earlier_failure.metadata.missing_module
                if (
                    earlier_failure.metadata.exception_type == "ModuleNotFoundError"
                    and module in earlier_failure.profile.required_modules
                ):
                    add(
                        C.ENVIRONMENT,
                        "Identical code/profile failed with a declared missing dependency and passed in a different registered environment; this is an environment contrast.",
                        previous,
                    )
                    next_check = "Verify the supplied dependency identity and repeat the unchanged code/profile in the repaired environment; do not edit repository code solely to hide the import."
            if (
                same_code
                and same_env
                and passed(focus)
                and previous.runner_reason in {"transport_unavailable", "temporary_service_error"}
            ):
                add(
                    C.TRANSIENT_TOOL,
                    "An explicitly recorded transport/service fault was followed by success with unchanged code, environment and profile; the transient explanation remains provisional.",
                    previous,
                )
                next_check = "Inspect the tool/service incident and side-effect confirmation; any bounded retry still requires the later execution policy."
        if (
            focus.profile
            and focus.profile.role == "dependency_probe"
            and focus.metadata
            and focus.metadata.dependency_available is False
        ):
            add(
                C.UNCERTAIN,
                "A dependency probe reports absence; absence alone does not establish that it caused this task failure.",
            )
        if (
            focus.profile
            and focus.profile.role == "localization_probe"
            and failed(focus)
            and focus.metadata
            and focus.metadata.target_hit is False
            and focus.metadata.failure_reproduced is True
        ):
            add(
                C.WRONG_LOCATION,
                "The controlled failing case did not execute the proposed target; reconsider that location rather than treating it as established.",
            )
            next_check = "Trace the failing input to a candidate that is actually executed, preserving the same failing check and environment."
        if (
            focus.profile
            and focus.profile.role == "reproduction"
            and passed(focus)
            and C.ENVIRONMENT not in labels
            and C.TRANSIENT_TOOL not in labels
        ):
            add(
                C.NOT_REPRODUCED,
                "The declared reproduction check passed in this observation; this does not prove that the issue is absent for other inputs.",
            )
            next_check = "Obtain a distinguishing input or environment description and investigate read-only before speculative edits."
    for left in expectations:
        if left.source_evidence_id not in facts:
            raise ValueError("expectation_requires_registered_source_evidence")
        for right in expectations:
            if (
                left.source != right.source
                and left.input_key == right.input_key
                and left.expected_value != right.expected_value
            ):
                add(
                    C.REQUIREMENT_AMBIGUITY,
                    "Operator-annotated issue/user and public-test expectations disagree for the same input; semantics require clarification.",
                )
                labels.add(C.UNCERTAIN)
                used.update((left.source_evidence_id, right.source_evidence_id))
                confidence = "low"
                next_check = "Present both quoted requirements and ask which behavior is intended; do not silently rewrite tests to choose a side."
                break
    if not labels:
        add(C.UNCERTAIN, "The available observations do not isolate environment, code, location or requirement causes.")
    if labels == {C.UNCERTAIN}:
        confidence = "low"
    if analyses:
        reasons.append(
            "Attributed model proposals are attached for review, not treated as observed facts or authority."
        )
    return Classification(
        task_id=focus.task_id,
        workspace_id=focus.workspace_id,
        workspace_version=focus.workspace_version,
        focus_evidence_id=focus.evidence_id,
        labels=tuple(sorted(labels)),
        confidence=confidence,
        reasons=tuple(dict.fromkeys(reasons)),
        evidence_ids=tuple(sorted(used, key=str)),
        model_analysis_ids=tuple(a.analysis_id for a in analyses),
        declared_expectations=expectations,
        next_distinguishing_check=next_check,
    )
