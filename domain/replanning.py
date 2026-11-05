"""Trusted opt-in policy configuration, not a model-assigned capability."""

from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.failure_diagnosis import DiagnosticProfile


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExpectationSource(Contract):
    path: str
    source: Literal["issue_or_user", "public_test"]
    input_key: str
    expected_value: str
    quote: str = Field(min_length=1, max_length=1000)


class ReplanSettings(Contract):
    enabled: bool = Field(default=False, strict=True)
    version: Literal["evidence-policy-v1"] = "evidence-policy-v1"
    max_corrections: int = Field(default=3, ge=1, le=6, strict=True)
    max_identical_observations: int = Field(default=2, ge=1, le=3, strict=True)
    max_safe_test_retries: int = Field(default=1, ge=0, le=2, strict=True)
    max_refused_actions: int = Field(default=2, ge=1, le=4, strict=True)
    environment_profile: str | None = None
    diagnostic_profiles: tuple[DiagnosticProfile, ...] = ()
    expectation_sources: tuple[ExpectationSource, ...] = ()

    @model_validator(mode="after")
    def unique_profiles(self):
        names = [p.profile_id for p in self.diagnostic_profiles]
        if len(set(names)) != len(names):
            raise ValueError("unique_replan_diagnostic_profiles_required")
        return self


class PolicyDecision(Contract):
    decision_id: UUID = Field(default_factory=uuid4)
    classification_id: UUID | None
    evidence_call_ids: tuple[UUID, ...]
    action: Literal[
        "retry_test",
        "diagnose_environment",
        "investigate_location",
        "correct_implementation",
        "check_regression",
        "clarify_requirement",
        "gather_evidence",
        "stop_unknown",
        "stop_no_progress",
        "stop_authorization",
        "stop_cancelled",
        "stop_limits",
    ]
    reason: str
    change: str
    permissions_expanded: Literal[False] = False


class ReplanRecord(Contract):
    """Evidence-backed change of strategy; repeated plans are rejected."""

    trigger_event_id: UUID
    failure_labels: tuple[str, ...] = Field(min_length=1)
    old_hypothesis_revision: int = Field(ge=0, strict=True)
    new_hypothesis_revision: int = Field(ge=0, strict=True)
    old_strategy: dict[str, Any]
    new_strategy: dict[str, Any]
    discriminating_check: str = Field(min_length=1, max_length=2000)
    expected_information_gain: float = Field(ge=0, le=1)
    budget_delta: int = 0
    stop_if_no_change: str = Field(min_length=1, max_length=1000)
    policy_hash: str = Field(min_length=1)

    @model_validator(mode="after")
    def strategy_must_change(self):
        changed_keys = {
            key
            for key in (
                "candidate_locations", "tool_type", "validation_target", "context_sources",
                "execution_order", "stop_condition",
            )
            if self.old_strategy.get(key) != self.new_strategy.get(key)
        }
        if not changed_keys:
            raise ValueError("replan_requires_strategy_change")
        if self.new_hypothesis_revision < self.old_hypothesis_revision:
            raise ValueError("replan_hypothesis_revision_must_advance")
        return self


# This inspectable table specifies decisions, not shell commands or permissions.
POLICY_TABLE = {
    "unknown_or_unconfirmed_side_effect": "stop_unknown",
    "confirmed_cleaned_isolated_timeout": "retry_test",
    "environment_or_declared_missing_dependency": "diagnose_environment",
    "wrong_location": "investigate_location",
    "regression": "check_regression",
    "implementation": "correct_implementation",
    "requirement_ambiguity": "clarify_requirement",
    "uncertain_or_not_reproduced": "gather_evidence",
    "same_input_code_environment_and_observation_without_information": "stop_no_progress",
    "authorization_changed": "stop_authorization",
    "cancelled": "stop_cancelled",
    "run_limits": "stop_limits",
}
