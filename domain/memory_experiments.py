"""Protocol availability is a dependency-complete ordinal prefix, distinct from historical and execution time."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


MemoryMode = Literal["none", "semantic", "semantic_episode", "all"]


class VisibleFact(Contract):
    fact_id: str = Field(pattern=r"^[a-zA-Z0-9_.:/-]{1,160}$")
    kind: Literal["public_source", "public_validation", "semantic", "episode", "skill", "transfer"]
    document_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_instance: str = Field(min_length=1, max_length=200)
    repo: str
    available_step: int = Field(ge=0, strict=True)
    historical_at: AwareDatetime | None
    executed_at: AwareDatetime
    dependencies: tuple[str, ...] = Field(default=(), max_length=64)
    origin: Literal["visible_task", "visible_development_validation", "operator_confirmation"]
    target_hidden_label_used: Literal[False] = False


class SequenceTask(Contract):
    instance_id: str = Field(min_length=1, max_length=200)
    repo: str
    step: int = Field(ge=1, strict=True)
    track: Literal["same_repository", "new_repository"]
    role: Literal["warmup", "target"]
    issue_created_at: AwareDatetime | None


class VisibilityManifest(Contract):
    protocol: Literal["controlled-memory-sequence-v1"] = "controlled-memory-sequence-v1"
    task: SequenceTask
    regime: Literal["frozen", "online"]
    mode: MemoryMode
    initial_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    facts: tuple[VisibleFact, ...] = Field(max_length=2048)
    allowed: tuple[str, ...] = Field(max_length=2048)
    prewarm_instances: tuple[str, ...] = Field(default=(), max_length=32)
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_reconstruction_claimed: Literal[False] = False
    pretraining_contamination_eliminated: Literal[False] = False

    @model_validator(mode="after")
    def complete_prefix(self):
        by_id = {f.fact_id: f for f in self.facts}
        if len(by_id) != len(self.facts) or len(set(self.allowed)) != len(self.allowed):
            raise ValueError("unique_visibility_facts_required")
        allowed = set(self.allowed)
        if not allowed <= set(by_id):
            raise ValueError("visibility_unknown_fact")
        visiting, visited = set(), set()

        def visit(key):
            if key in visiting:
                raise ValueError("visibility_dependency_cycle")
            if key in visited:
                return
            visiting.add(key)
            fact = by_id[key]
            if fact.source_instance == self.task.instance_id or fact.available_step >= self.task.step:
                raise ValueError("future_or_current_target_fact_leak")
            if any(dep not in allowed for dep in fact.dependencies):
                raise ValueError("visibility_missing_dependency_or_late_validation")
            for dep in fact.dependencies:
                if by_id[dep].available_step > fact.available_step:
                    raise ValueError("memory_backdated_before_supporting_evidence")
                visit(dep)
            visiting.remove(key)
            visited.add(key)

        for key in self.allowed:
            visit(key)
        return self
