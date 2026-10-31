"""Evaluator-only rules and results. Never import these records into Agent inputs."""

import hashlib
import json
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from domain.workspace_tools import TaskPackage


def content_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    ).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthorityCase(Contract):
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    group: Literal["FAIL_TO_PASS", "PASS_TO_PASS"]
    probe: str = Field(min_length=1, max_length=1024)
    expected: Any
    max_seconds: int = Field(default=5, gt=0, le=30, strict=True)

    @model_validator(mode="after")
    def serializable(self):
        compile(self.probe, "<evaluator-probe>", "exec")
        if len(json.dumps(self.expected, ensure_ascii=False, allow_nan=False).encode()) > 4096:
            raise ValueError("expected_value_limit")
        return self


class EvaluationSpec(Contract):
    schema_version: Literal[1] = 1
    protocol_version: Literal["behavior-oracle-v1"] = "behavior-oracle-v1"
    package: TaskPackage
    cases: tuple[AuthorityCase, ...] = Field(min_length=2, max_length=128)
    origin: Literal["synthetic_development", "curator_development", "formal_frozen"]

    @model_validator(mode="after")
    def authoritative_inventory(self):
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValueError("duplicate_authority_case")
        if {c.group for c in self.cases} != {"FAIL_TO_PASS", "PASS_TO_PASS"}:
            raise ValueError("both_f2p_p2p_required")
        if self.origin == "formal_frozen":
            raise ValueError("formal_admission_and_protocol_freeze_not_yet_supported")
        if (self.origin == "synthetic_development") != (self.package.task.source_kind == "contract_fixture"):
            raise ValueError("evaluation_origin_mismatch")
        return self


class PatchSubmission(Contract):
    task_id: UUID
    instance_id: str = Field(min_length=1, max_length=200)
    method_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    repeat_index: int = Field(ge=0, strict=True)
    patch: str = Field(max_length=1048576)


class CaseResult(Contract):
    case_id: str
    group: Literal["FAIL_TO_PASS", "PASS_TO_PASS"]
    state: Literal["passed", "failed", "missing", "skipped", "timeout", "environment_error"]
    actual: Any = None
    call_id: UUID | None = None
    reason: str | None = None


class EvaluationResult(Contract):
    schema_version: Literal[1] = 1
    eval_run_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    instance_id: str
    method_id: str
    repeat_index: int
    origin: Literal["synthetic_development", "curator_development", "formal_frozen"]
    base_commit: str | None
    base_content_hash: str
    patch_hash: str
    effective_patch_hash: str | None = None
    patch_state: Literal["empty", "nonempty", "invalid"]
    image_id: str
    evaluator_version: Literal["behavior-oracle-v1", "swebench-kernel-v1"] = "behavior-oracle-v1"
    evaluator_digest: str
    rules_hash: str
    recorded_at: AwareDatetime
    baseline: tuple[CaseResult, ...] = ()
    candidate: tuple[CaseResult, ...] = ()
    status: Literal["resolved", "unresolved", "unscorable", "environment_error", "invalid_patch"]
    regression: Literal["observed", "not_observed", "unconfirmed"]
    reason: str
    result_cache_reused: Literal[False] = False
    agent_feedback_allowed: Literal[False] = False

    @model_validator(mode="after")
    def resolved_requires_evidence(self):
        if self.status == "resolved" and (
            self.patch_state != "nonempty"
            or self.regression != "not_observed"
            or not self.candidate
            or any(case.state != "passed" for case in self.candidate)
            or any(case.call_id is None for case in (*self.baseline, *self.candidate))
            or len(self.baseline) != len(self.candidate)
            or {case.case_id for case in self.baseline} != {case.case_id for case in self.candidate}
            or {case.group for case in self.baseline} != {"FAIL_TO_PASS", "PASS_TO_PASS"}
            or any(case.state != ("failed" if case.group == "FAIL_TO_PASS" else "passed") for case in self.baseline)
        ):
            raise ValueError("resolved_requires_nonempty_patch_and_executed_passing_cases")
        return self
