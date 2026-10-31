"""Auditable same-code repair baseline with a trusted opt-in replanning policy."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from domain.context import BrowseArtifact
from domain.episodic_memory import AssessEpisode, BrowseEpisode, SearchEpisodes
from domain.plans import CandidateLocation, PlanScope, path_subset
from domain.procedural_memory import PlanSkill
from domain.reproduction import PublicReproducerBinding
from domain.workspace_tools import Arguments
from domain.workspaces import relative_parts


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepairRegistration(Contract):
    scope: PlanScope
    reproduction_profile: str
    required_profiles: tuple[str, ...] = Field(min_length=1, max_length=8)
    protected_paths: tuple[str, ...] = Field(min_length=1)
    max_context_bytes: int = Field(default=196608, ge=8192, le=524288, strict=True)
    method_id: Literal["native-react-v1"] = "native-react-v1"
    # Future mechanisms have their own modules; these flags identify this baseline.
    replan_policy: bool = Field(default=False, strict=True)
    recovery: Literal[False] = False
    memory: Literal[False] = False
    public_reproducer: PublicReproducerBinding | None = None

    @model_validator(mode="after")
    def completion_contract(self):
        if self.reproduction_profile not in self.required_profiles:
            raise ValueError("reproduction_must_be_required_for_completion")
        if len(set(self.required_profiles)) != len(self.required_profiles):
            raise ValueError("unique_completion_profiles_required")
        if not set(self.required_profiles) <= set(self.scope.test_profiles):
            raise ValueError("completion_profiles_must_be_in_scope")
        if not {"read", "edit", "run_test", "diff"} <= self.scope.operations:
            raise ValueError("repair_operations_required")
        for path in self.protected_paths:
            relative_parts(path)
        if any(
            path_subset((protected,), (write,)) or path_subset((write,), (protected,))
            for protected in self.protected_paths
            for write in self.scope.write_paths
        ):
            raise ValueError("repair_write_scope_overlaps_protected_paths")
        if self.public_reproducer is not None and self.public_reproducer.probe_path not in self.protected_paths:
            raise ValueError("public_reproducer_must_be_protected")
        return self


class PlanDraft(Contract):
    understanding: str = Field(min_length=1, max_length=4000)
    candidates: tuple[CandidateLocation, ...] = Field(min_length=1, max_length=32)
    hypotheses: tuple[str, ...] = Field(min_length=1, max_length=16)
    change_strategy: tuple[str, ...] = Field(min_length=1, max_length=32)
    validation_strategy: tuple[str, ...] = Field(min_length=1, max_length=16)


class ProposePlan(Contract):
    operation: Literal["plan"]
    draft: PlanDraft


class InvokeTool(Contract):
    operation: Literal["tool"]
    arguments: Arguments


class ReplaceText(Contract):
    operation: Literal["replace"]
    path: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_text: str = Field(min_length=1, max_length=262144)
    new_text: str = Field(max_length=262144)


class Finish(Contract):
    operation: Literal["finish"]
    summary: str = Field(min_length=1, max_length=4000)


class NeedInput(Contract):
    operation: Literal["need_input"]
    reason: str = Field(min_length=1, max_length=2000)


AgentAction = Annotated[
    ProposePlan
    | InvokeTool
    | ReplaceText
    | Finish
    | NeedInput
    | BrowseArtifact
    | SearchEpisodes
    | BrowseEpisode
    | AssessEpisode
    | PlanSkill,
    Field(discriminator="operation"),
]
ACTION: TypeAdapter = TypeAdapter(AgentAction)
