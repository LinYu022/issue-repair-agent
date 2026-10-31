"""Operator-authored visible reproducer bound before inference; no authority inputs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROBE_PATH: Literal["_issue_public_probe.py"] = "_issue_public_probe.py"


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PublicReproducerBinding(Contract):
    schema_version: Literal[1] = 1
    task_id: str
    repo: str
    base_commit: str | None
    issue_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_source_version: str = Field(pattern=r"^[0-9a-f]{64}$")
    probe_path: Literal["_issue_public_probe.py"] = PROBE_PATH
    code_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: Literal["operator_authored_from_visible_issue"] = "operator_authored_from_visible_issue"
    rationale: str = Field(min_length=1, max_length=2000)


class PublicReproducerSpec(PublicReproducerBinding):
    code: str = Field(min_length=1, max_length=16384)

    @model_validator(mode="after")
    def valid_code(self):
        import ast
        import hashlib

        raw = self.code.encode("utf-8")
        if len(raw) > 16384 or "\0" in self.code or hashlib.sha256(raw).hexdigest() != self.code_sha256:
            raise ValueError("public_reproducer_code_integrity_or_size")
        try:
            ast.parse(self.code)
        except SyntaxError as error:
            raise ValueError("public_reproducer_syntax_error") from error
        return self
