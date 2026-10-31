"""Provenance-bearing observations and revisable hypotheses; never hidden labels."""

from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from domain.models import ArtifactRef


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FailureCategory(StrEnum):
    ENVIRONMENT = "environment"
    TRANSIENT_TOOL = "transient_tool"
    NOT_REPRODUCED = "not_reproduced"
    WRONG_LOCATION = "wrong_location"
    IMPLEMENTATION = "implementation"
    REGRESSION = "regression"
    REQUIREMENT_AMBIGUITY = "requirement_ambiguity"
    UNCERTAIN = "uncertain"


class ExecutionContext(Contract):
    environment_id: str
    image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    container_limits_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class DiagnosticProfile(Contract):
    profile_id: str
    logical_check_id: str
    role: Literal["reproduction", "regression", "dependency_probe", "localization_probe"]
    profile_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_format: Literal["none", "fixture-json-v1"] = "none"
    required_modules: tuple[str, ...] = ()


class TestMetadata(Contract):
    schema_version: Literal[1] = 1
    check_id: str
    outcome: Literal["passed", "failed"]
    exception_type: str | None = None
    missing_module: str | None = None
    dependency_available: bool | None = None
    target_hit: bool | None = None
    failure_reproduced: bool | None = None
    # Reported by task-side Python. Integrity of the bytes is not proof that a
    # malicious repository truthfully describes its semantics.
    provenance: Literal["task_process_report"] = "task_process_report"


class DiagnosticEvidence(Contract):
    schema_version: Literal[1] = 1
    evidence_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    workspace_id: UUID
    workspace_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    call_id: UUID
    operation: str
    status: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]
    exit_code: int | None
    source_artifact: ArtifactRef
    context: ExecutionContext | None
    profile: DiagnosticProfile | None = None
    metadata: TestMetadata | None = None
    metadata_state: Literal["not_requested", "valid", "invalid", "legacy_context_missing"]
    runner_reason: str | None = None
    late_result: bool = False
    recorded_at: AwareDatetime
    evidence_kind: Literal["real_execution", "simulation"]


class ModelAnalysis(Contract):
    analysis_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    call_id: UUID
    workspace_version: str
    claim: str = Field(min_length=1, max_length=4000)
    source_artifact: ArtifactRef
    evidence_kind: Literal["real_execution", "simulation"]
    status: Literal["unverified_model_proposal"] = "unverified_model_proposal"


class DeclaredExpectation(Contract):
    source_evidence_id: UUID
    source: Literal["issue_or_user", "public_test"]
    input_key: str = Field(min_length=1, max_length=200)
    expected_value: str = Field(max_length=500)
    quote: str = Field(min_length=1, max_length=1000)
    interpretation: Literal["operator_annotated_not_machine_proven_semantics"] = (
        "operator_annotated_not_machine_proven_semantics"
    )


class Classification(Contract):
    classification_id: UUID = Field(default_factory=uuid4)
    classifier_version: Literal["conservative-failure-v1"] = "conservative-failure-v1"
    task_id: UUID
    workspace_id: UUID
    workspace_version: str
    focus_evidence_id: UUID
    labels: tuple[FailureCategory, ...] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]
    reasons: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[UUID, ...] = Field(min_length=1)
    model_analysis_ids: tuple[UUID, ...] = ()
    declared_expectations: tuple[DeclaredExpectation, ...] = ()
    next_distinguishing_check: str = Field(min_length=1, max_length=2000)
    causal_root_proven: Literal[False] = False
    actions_executed: Literal[0] = 0


class DistinguishingCheck(Contract):
    question: str = Field(min_length=1, max_length=2000)
    fixed_factors: tuple[str, ...] = Field(min_length=1)
    changed_factor: str = Field(min_length=1, max_length=500)
    expected_if_supported: str = Field(min_length=1, max_length=1000)
    expected_if_refuted: str = Field(min_length=1, max_length=1000)
    requires_current_authorization: Literal[True] = True
    executed: Literal[False] = False


class Attempt(Contract):
    action_evidence_id: UUID
    outcome_evidence_id: UUID
    interpretation: str = Field(min_length=1, max_length=2000)


class HypothesisRevision(Contract):
    revision_id: UUID = Field(default_factory=uuid4)
    hypothesis_id: UUID
    parent_revision_id: UUID | None
    parent_hash: str | None
    task_id: UUID
    workspace_id: UUID
    subject_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_environment_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    claim: str = Field(min_length=1, max_length=4000)
    categories: tuple[FailureCategory, ...] = Field(min_length=1)
    supporting_evidence_ids: tuple[UUID, ...] = ()
    opposing_evidence_ids: tuple[UUID, ...] = ()
    model_analysis_ids: tuple[UUID, ...] = ()
    attempts: tuple[Attempt, ...] = ()
    status: Literal["proposed", "supported", "inconclusive", "refuted", "superseded"]
    confidence: Literal["low", "medium"]
    next_check: DistinguishingCheck
    revision_reason: str = Field(min_length=1, max_length=2000)
    created_at: AwareDatetime
    author: Literal["local_operator", "development_fixture", "classifier"]

    @model_validator(mode="after")
    def honest_status(self):
        if self.status == "supported" and (not self.supporting_evidence_ids or self.opposing_evidence_ids):
            raise ValueError("supported_requires_observations_without_unresolved_counterevidence")
        if self.status == "refuted" and not self.opposing_evidence_ids:
            raise ValueError("refuted_requires_counterevidence")
        if self.status in {"proposed", "refuted", "superseded"} and self.confidence != "low":
            raise ValueError("inactive_or_unverified_hypothesis_cannot_claim_confidence")
        if (self.parent_revision_id is None) != (self.parent_hash is None):
            raise ValueError("revision_parent_identity_and_hash_required_together")
        if set(self.supporting_evidence_ids) & set(self.opposing_evidence_ids):
            raise ValueError("evidence_cannot_support_and_oppose_same_revision")
        return self
