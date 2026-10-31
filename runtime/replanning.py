"""Bounded policy over P2-07 evidence. No evaluator access, new grants or recovery."""

import base64
import json
import re
import time
from collections import deque
from dataclasses import asdict
from typing import TYPE_CHECKING
from uuid import UUID

from domain.failure_diagnosis import (
    Classification,
    DeclaredExpectation,
    DiagnosticEvidence,
    DiagnosticProfile,
    DistinguishingCheck,
    HypothesisRevision,
    ModelAnalysis,
)
from domain.failure_diagnosis import FailureCategory as C
from domain.plans import document_hash
from domain.replanning import POLICY_TABLE, PolicyDecision, ReplanSettings
from domain.tasks import TaskLifecycle
from runtime.diagnostic_evidence import DiagnosticEvidenceStore, environment_hash
from runtime.failure_classifier import classify_failure
from runtime.hypothesis_ledger import HypothesisLedger
from tools.readonly_workspace import WorkspaceRefused

if TYPE_CHECKING:
    from runtime.repair_loop import RepairLoop


class RepeatDetector:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.seen: dict[str, tuple[str, int, int]] = {}
        self.distinct_observations: set[str] = set()
        self.evidence_epoch = 0

    @staticmethod
    def key(arguments: dict, code_version: str, context: dict):
        return document_hash({"arguments": arguments, "code": code_version, "context": context})

    def blocked(self, key: str):
        _value, count, epoch = self.seen.get(key, ("", 0, -1))
        return count >= self.maximum and epoch == self.evidence_epoch

    def observe(self, key: str, summary: dict):
        value = document_hash(summary)
        identity = document_hash({"action_state": key, "observation": value})
        if identity not in self.distinct_observations:
            self.distinct_observations.add(identity)
            self.evidence_epoch += 1
        old, count, epoch = self.seen.get(key, ("", 0, -1))
        self.seen[key] = (value, count + 1 if old == value and epoch == self.evidence_epoch else 1, self.evidence_epoch)


class Replanner:
    def __init__(self, loop: "RepairLoop", settings: ReplanSettings, *, reopen_records=False):
        self.loop, self.session, self.settings = loop, loop.session, settings
        self.enabled = settings.enabled
        self.root = loop.root / "replanning"
        self.root.mkdir(exist_ok=reopen_records)
        (self.root / "settings.json").write_text(settings.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (self.root / "policy-table.json").write_text(json.dumps(POLICY_TABLE, indent=2) + "\n", encoding="utf-8")
        self.store = DiagnosticEvidenceStore(self.session.records)
        self.ledger = HypothesisLedger(self.store)
        self.facts: list[DiagnosticEvidence] = []
        self.expectations: dict[str, DeclaredExpectation] = {}
        self.pending: deque[dict] = deque()
        self.detector = RepeatDetector(settings.max_identical_observations)
        self.retries: dict[str, int] = {}
        self.refusals = self.corrections = self.decisions = self.revisions = 0
        self.pin: tuple | None = None
        self.needs_plan = False
        self.investigated = False
        self.require_readonly = False
        self.initial_baselines_scheduled = False
        self.hypothesis: HypothesisRevision | None = None
        self.last_classification: Classification | None = None
        self.last_evidence: DiagnosticEvidence | None = None
        self.trigger_evidence: DiagnosticEvidence | None = None
        self.model_call_id: UUID | None = None
        self.decision: PolicyDecision | None = None
        self.profiles = {p.profile_id: p for p in settings.diagnostic_profiles}
        for name, profile in self.session.profiles.items():
            if name not in self.profiles:
                self.profiles[name] = DiagnosticProfile(
                    profile_id=name,
                    logical_check_id=name,
                    role="reproduction" if name == loop.registration.reproduction_profile else "regression",
                    profile_hash=document_hash(profile),
                )
            if self.profiles[name].profile_hash != document_hash(profile):
                raise ValueError("replan_profile_hash_not_operator_registered")
        if settings.environment_profile and settings.environment_profile not in loop.registration.scope.test_profiles:
            raise ValueError("replan_environment_profile_not_granted")

    def context(self, arguments):
        profile = self.session.profiles.get(arguments.get("profile_id"))
        return {
            "environment_id": self.session.task.environment_id,
            "image": self.session.runner.image,
            "resources": document_hash(asdict(self.session.runner.limits)),
            "profile": None if profile is None else document_hash(profile),
        }

    def record(self, action, reason, change, fact=None, classification=None):
        value = PolicyDecision(
            classification_id=None if classification is None else classification.classification_id,
            evidence_call_ids=() if fact is None else (fact.call_id,),
            action=action,
            reason=reason,
            change=change,
        )
        self.decisions += 1
        self.decision = value
        if fact is not None:
            self.trigger_evidence = fact
        self.loop._event("strategy_decision", decision=value.model_dump(mode="json"))
        return value

    def stop(self, action, reason, lifecycle="pause"):
        self.record(
            action,
            reason,
            "Stop new dispatch and preserve the current patch and evidence",
            self.last_evidence,
            self.last_classification,
        )
        return self.loop.seal("policy_" + reason, lifecycle)

    def guard(self):
        if not self.enabled or self.loop.closed:
            return
        task = self.session.task
        if self.session.cancelled.is_set() or task.lifecycle == TaskLifecycle.CANCELLED:
            self.stop("stop_cancelled", "cancelled")
            raise WorkspaceRefused("policy_cancelled")
        if time.monotonic() - self.session.started >= task.limits.max_wall_seconds:
            self.stop("stop_limits", "wall_limit")
            raise WorkspaceRefused("policy_wall_limit")
        if self.loop.controller.mode == "EXECUTE":
            auth = self.loop.controller.authorization
            pin = (
                None if auth is None else auth.authorization_id,
                task.grant_id,
                task.execution_epoch,
                task.repo,
                task.base_commit,
                self.session.task.environment_id,
                self.session.runner.image,
                document_hash(asdict(self.session.runner.limits)),
                document_hash({k: v.model_dump(mode="json") for k, v in self.session.profiles.items()}),
            )
            if self.pin is None:
                self.pin = pin
            if pin != self.pin or not self.loop.controller.view()["authorization_active"]:
                self.stop("stop_authorization", "authorization_changed", "wait_input")
                raise WorkspaceRefused("policy_authorization_changed")
        elif self.pin is not None:
            self.stop("stop_authorization", "authorization_no_longer_executing", "wait_input")
            raise WorkspaceRefused("policy_authorization_changed")

    def before_tool(self, arguments):
        if not self.enabled:
            return None
        self.guard()
        operation = arguments.get("operation")
        if operation == "edit" and (self.needs_plan or self.require_readonly):
            raise WorkspaceRefused("policy_current_evidence_requires_investigation_or_plan")
        if self.session.task.workspace_version is None:
            raise WorkspaceRefused("policy_workspace_version_missing")
        if operation == "read" and arguments.get("start_line") is not None:
            observations = self.loop.context_manager.observations
            last = observations[-1] if observations else None
            if (
                last is not None
                and last["code"] is not None
                and last["request"].arguments.start_line is not None
                and len(last["payload"]["result"]["text"].encode())
                <= min(self.loop.context_manager.settings.max_context_bytes, self.loop.registration.max_context_bytes)
                // 2
                and last["request"].arguments.path == arguments["path"]
                and last["request"].expected_version == self.session.task.workspace_version
                and last["request"].execution_epoch == self.session.task.execution_epoch
                and last["payload"]["result"]["start_line"] <= arguments["start_line"]
                and arguments["end_line"] <= last["payload"]["result"]["end_line"]
            ):
                raise WorkspaceRefused("read_range_already_observed")
        key = self.detector.key(arguments, self.session.task.workspace_version, self.context(arguments))
        if self.detector.blocked(key):
            self.stop("stop_no_progress", "no_information_repeat")
            raise WorkspaceRefused("policy_no_information_repeat")
        return key

    def _hypothesis_update(self, fact, status, reason):
        if self.hypothesis is None:
            return
        old = self.hypothesis
        fields = old.model_dump(
            exclude={"revision_id", "hypothesis_id", "parent_revision_id", "parent_hash", "created_at"}
        )
        fields.update(
            subject_version=fact.workspace_version,
            subject_environment_hash=environment_hash(fact.context),
            status=status,
            confidence="low",
            revision_reason=reason,
            supporting_evidence_ids=() if status == "refuted" else (fact.evidence_id,),
            opposing_evidence_ids=(fact.evidence_id,) if status == "refuted" else (),
        )
        self.hypothesis = self.ledger.append(
            hypothesis_id=old.hypothesis_id, expected_parent_id=old.revision_id, **fields
        )

    def plan(self, draft, previous):
        if not self.enabled:
            return
        if self.needs_plan:
            if not self.investigated:
                raise WorkspaceRefused("replan_requires_fresh_investigation")
            if previous and (draft.change_strategy, draft.hypotheses, draft.candidates) == (
                previous.body.change_strategy,
                previous.body.hypotheses,
                previous.body.candidates,
            ):
                raise WorkspaceRefused("replan_requires_explicit_strategy_or_hypothesis_change")

    def plan_saved(self, draft, previous):
        if not self.enabled:
            return
        if self.hypothesis and self.last_evidence:
            self._hypothesis_update(
                self.last_evidence, "superseded", "A newer explicit plan replaces this working hypothesis"
            )
        analyses: tuple[ModelAnalysis, ...] = (
            () if self.model_call_id is None else self.store.capture_model_plans(self.model_call_id)
        )
        context_hash = (
            document_hash({"context": "planning_before_test"})
            if self.last_evidence is None
            else environment_hash(self.last_evidence.context)
        )
        self.hypothesis = self.ledger.append(
            task_id=self.session.task.task_id,
            workspace_id=self.session.identity.workspace_id,
            subject_version=self.session.task.workspace_version,
            subject_environment_hash=context_hash,
            claim=draft.hypotheses[0],
            categories=(C.UNCERTAIN,),
            supporting_evidence_ids=(),
            opposing_evidence_ids=(),
            model_analysis_ids=tuple(a.analysis_id for a in analyses),
            status="proposed",
            confidence="low",
            author="classifier",
            revision_reason="Model-proposed strategy, not established root cause",
            next_check=DistinguishingCheck(
                question="Which current observation distinguishes the active hypotheses?",
                fixed_factors=("environment", "public input"),
                changed_factor="candidate implementation or investigated location",
                expected_if_supported="prediction follows the candidate",
                expected_if_refuted="prediction fails or target is not involved",
            ),
        )
        if self.needs_plan:
            self.revisions += 1
            self.loop._event(
                "replan_applied",
                evidence_call_ids=[] if self.trigger_evidence is None else [str(self.trigger_evidence.call_id)],
                classification_id=None
                if self.decision is None or self.decision.classification_id is None
                else str(self.decision.classification_id),
                previous_revision=None if previous is None else str(previous.revision_id),
                new_revision=str(self.session.task.revision_id),
                before_strategy=[] if previous is None else list(previous.body.change_strategy),
                after_strategy=list(draft.change_strategy),
                before_hypotheses=[] if previous is None else list(previous.body.hypotheses),
                after_hypotheses=list(draft.hypotheses),
                before_candidates=[]
                if previous is None
                else [c.model_dump(mode="json") for c in previous.body.candidates],
                after_candidates=[c.model_dump(mode="json") for c in draft.candidates],
                reason=None if self.decision is None else self.decision.reason,
            )
        self.needs_plan = self.investigated = False

    def after_tool(self, request, observation, payload, key):
        if not self.enabled:
            return
        profile = self.profiles.get(getattr(request.arguments, "profile_id", ""))
        fact = self.store.capture(request.call_id, profile)
        self.facts.append(fact)
        self.last_evidence = fact
        result = payload.get("result")
        run = payload.get("run", {})
        self.detector.observe(
            key,
            {
                "status": fact.status,
                "exit_code": fact.exit_code,
                "reason": fact.runner_reason,
                "result": result,
                "metadata": None if fact.metadata is None else fact.metadata.model_dump(mode="json"),
                "output": None
                if fact.metadata is not None
                else {
                    stream: re.sub(
                        r"\b\d+(?:\.\d+)?s\b",
                        "<duration>",
                        base64.b64decode(run.get(stream + "_base64", "")).decode("utf-8", errors="replace"),
                    )
                    for stream in ("stdout", "stderr")
                },
            },
        )
        operation = request.arguments.operation
        if operation in {"read", "search", "symbols", "investigate"} and observation.status.value == "SUCCEEDED":
            if self.needs_plan:
                self.investigated = True
            if operation == "read":
                for source in self.settings.expectation_sources:
                    if request.arguments.path == source.path and source.quote in payload.get("result", {}).get(
                        "text", ""
                    ):
                        self.expectations[source.path] = DeclaredExpectation(
                            source_evidence_id=fact.evidence_id, **source.model_dump(exclude={"path"})
                        )
        if self.session.blocked or fact.status == "UNKNOWN" or fact.late_result:
            self.stop("stop_unknown", "outcome_requires_inspection")
            return
        if operation != "run_test" and len(self.expectations) < 2:
            return
        value = classify_failure(fact, tuple(self.facts[:-1]), expectations=tuple(self.expectations.values()))
        self.ledger.save_classification(value)
        self.last_classification = value
        if C.REQUIREMENT_AMBIGUITY in value.labels:
            self.record(
                "clarify_requirement",
                "Quoted requirements conflict; selecting one silently would hide ambiguity",
                "Wait for clarification; do not rewrite tests",
                fact,
                value,
            )
            self.loop.seal("policy_requirement_ambiguity", "wait_input")
            return
        if operation != "run_test":
            return
        if (
            fact.status == "FAILED"
            and fact.runner_reason in {"timeout", "temporary_service_error", "transport_unavailable"}
            and run.get("cleanup_confirmed")
        ):
            retries = self.retries.get(key, 0)
            if retries < self.settings.max_safe_test_retries:
                self.retries[key] = retries + 1
                self.pending.appendleft(request.arguments.model_dump(mode="json"))
                self.record(
                    "retry_test",
                    "Confirmed cleaned isolated test has a registered transient fault code or timeout; the root cause remains unproven",
                    "One bounded same-state retry, retaining both attempts and budgets",
                    fact,
                    value,
                )
            else:
                self.stop("stop_no_progress", "safe_retry_limit")
            return
        if fact.status == "SUCCEEDED":
            if profile and profile.role == "dependency_probe" and self.require_readonly:
                self.record(
                    "diagnose_environment",
                    "Read-only environment verification completed; repair still requires operator action",
                    "Wait for registered environment repair or clarification",
                    fact,
                    value,
                )
                self.loop.seal("policy_environment_requires_operator", "wait_input")
            return
        if fact.status != "FAILED":
            return
        declared_missing = bool(
            fact.metadata
            and fact.metadata.exception_type == "ModuleNotFoundError"
            and profile
            and fact.metadata.missing_module in profile.required_modules
        )
        if C.ENVIRONMENT in value.labels or declared_missing:
            self.require_readonly = True
            self.record(
                "diagnose_environment",
                "Declared missing-dependency evidence calls for environment verification, not speculative code edits",
                "Run only the registered read-only environment check, then wait for operator",
                fact,
                value,
            )
            if self.settings.environment_profile:
                self.pending.appendleft({"operation": "run_test", "profile_id": self.settings.environment_profile})
            else:
                self.loop.seal("policy_environment_check_unavailable", "wait_input")
            return
        if C.WRONG_LOCATION in value.labels:
            self._hypothesis_update(fact, "refuted", "Failing input did not reach the proposed location")
            self.needs_plan, self.investigated = True, False
            self.pending.appendleft({"operation": "investigate", "path": "."})
            self.record(
                "investigate_location",
                "The failing case refutes the proposed location",
                "Investigate again and submit a changed in-scope plan tied to this evidence",
                fact,
                value,
            )
        elif self.loop._changed():
            self.corrections += 1
            if self.corrections > self.settings.max_corrections:
                self.stop("stop_no_progress", "correction_limit")
                return
            self._hypothesis_update(
                fact, "inconclusive", "Failed post-edit check requires revision of the working explanation"
            )
            if C.REGRESSION in value.labels:
                self.pending.appendleft({"operation": "diff", "path": "."})
                self.record(
                    "check_regression",
                    "A registered regression changed from pass to fail under the same environment",
                    "Inspect the patch impact, then allow bounded local correction and rerun all required checks",
                    fact,
                    value,
                )
            else:
                self.record(
                    "correct_implementation",
                    "A post-edit check still fails; this does not justify repeating the same edit blindly",
                    "Allow bounded current-scope correction with a fresh read and all required tests",
                    fact,
                    value,
                )
        else:
            self.record(
                "gather_evidence",
                "Baseline behavior failed; root cause remains provisional",
                "Read source and test; any edit is a bounded hypothesis experiment within the approved scope",
                fact,
                value,
            )
        if not self.initial_baselines_scheduled and not self.loop._changed():
            self.initial_baselines_scheduled = True
            self.pending.extend(
                {"operation": "run_test", "profile_id": p}
                for p in self.loop.registration.required_profiles
                if p != self.loop.registration.reproduction_profile
            )

    def refused(self, action, error):
        if not self.enabled or self.loop.closed:
            return
        self.refusals += 1
        text = str(error)
        if any(
            token in text
            for token in (
                "path_not_granted",
                "path_outside_current_plan",
                "repair_source_path_not_editable",
                "operation_not_granted",
                "test_outside_current_plan",
            )
        ):
            self.stop("stop_authorization", "requested_action_outside_scope", "wait_input")
        elif self.refusals >= self.settings.max_refused_actions:
            self.stop("stop_no_progress", "repeated_refused_actions")

    def feedback(self):
        return {
            "policy_instruction": None if self.decision is None else self.decision.model_dump(mode="json"),
            "requires_changed_plan": self.needs_plan,
            "edit_blocked_for_environment": self.require_readonly,
            "next_scheduled_tools": list(self.pending),
            "classification": None
            if self.last_classification is None
            else self.last_classification.model_dump(mode="json"),
        }

    def summary(self):
        return {
            "enabled": self.enabled,
            "settings_hash": document_hash(self.settings),
            "decisions": self.decisions,
            "plan_revisions_applied": self.revisions,
            "post_edit_corrections": self.corrections,
            "safe_test_retries": sum(self.retries.values()),
            "refused_actions": self.refusals,
            "permission_expansions": 0,
            "independent_evaluator_read": False,
        }
