"""Versioned workflows are evidence-backed plan templates, never execution authority."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain.episodic_memory import EpisodeRef
from domain.semantic_memory import MemoryScope
from domain.workspaces import relative_parts


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SkillParameter(Contract):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    kind: Literal["path", "registered_profile"]
    allowed: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def bounded_values(self):
        if len(set(self.allowed)) != len(self.allowed):
            raise ValueError("unique_skill_parameter_values")
        for value in self.allowed:
            if self.kind == "path":
                relative_parts(value)
            elif not value or len(value) > 64 or not all(c.isalnum() or c in "_-" for c in value):
                raise ValueError("registered_skill_profile_required")
        return self


class SkillStep(Contract):
    operation: Literal["read", "run_test"]
    parameter: str


class SkillDocument(Contract):
    schema_version: Literal[1] = 1
    skill_id: UUID
    revision: int = Field(ge=1, strict=True)
    user_id: UUID
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    purpose: str = Field(min_length=1, max_length=1000)
    scope: MemoryScope
    parameters: tuple[SkillParameter, ...] = Field(min_length=2, max_length=8)
    allowed_tools: tuple[Literal["read", "run_test"], ...]
    steps: tuple[SkillStep, ...] = Field(min_length=2, max_length=8)
    preconditions: tuple[Literal["files_present", "registered_profiles", "exact_environment", "read_scope"], ...]
    postconditions: tuple[Literal["all_tests_passed", "workspace_unchanged"], ...]
    failure_return: Literal["stop_and_report_evidence"] = "stop_and_report_evidence"
    sources: tuple[EpisodeRef, ...] = Field(min_length=2, max_length=8)
    parent_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    validation_boundary: Literal["separate_registered_visible_tasks_only"] = "separate_registered_visible_tasks_only"
    grants_authority: Literal[False] = False

    @model_validator(mode="after")
    def complete_flow(self):
        if self.scope.environment_hash is None or self.scope.workspace_version is not None:
            raise ValueError("skill_requires_environment_not_fixed_source_code")
        parameters = {p.name: p for p in self.parameters}
        if len(parameters) != len(self.parameters):
            raise ValueError("unique_skill_parameters_required")
        if set(self.allowed_tools) != {s.operation for s in self.steps} or len(set(self.allowed_tools)) != len(
            self.allowed_tools
        ):
            raise ValueError("skill_tool_inventory_mismatch")
        if self.steps[0].operation != "read" or not any(s.operation == "run_test" for s in self.steps):
            raise ValueError("skill_read_then_validate_required")
        used = set()
        for step in self.steps:
            parameter = parameters.get(step.parameter)
            if parameter is None or parameter.kind != ("path" if step.operation == "read" else "registered_profile"):
                raise ValueError("skill_typed_parameter_required")
            used.add(step.parameter)
        if used != set(parameters):
            raise ValueError("unused_skill_parameters")
        if set(self.preconditions) != {"files_present", "registered_profiles", "exact_environment", "read_scope"}:
            raise ValueError("skill_preconditions_required")
        if set(self.postconditions) != {"all_tests_passed", "workspace_unchanged"}:
            raise ValueError("skill_postconditions_required")
        if len({s.episode_id for s in self.sources}) != len(self.sources):
            raise ValueError("distinct_repeated_sources_required")
        if (self.revision == 1) != (self.parent_hash is None):
            raise ValueError("skill_parent_version_required")
        return self


class SkillRef(Contract):
    skill_id: UUID
    revision: int = Field(ge=1, strict=True)
    document_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SkillDecision(Contract):
    request_id: UUID
    target: SkillRef
    expected_enabled: SkillRef | None
    action: Literal["enable", "deprecate", "revoke", "rollback"]
    reason: str = Field(min_length=1, max_length=1000)


class PlanSkill(Contract):
    operation: Literal["plan_skill"] = "plan_skill"
    target: SkillRef
    parameters: dict[str, str] = Field(min_length=2, max_length=8)


class SkillValidationTask(Contract):
    task_id: UUID
    instance_id: str = Field(min_length=1, max_length=200)
    suite: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    visibility: Literal["exposed_development"] = "exposed_development"
    hidden_labels: Literal[False] = False
