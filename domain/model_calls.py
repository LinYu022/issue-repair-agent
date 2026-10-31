"""Model identities and confirmed/unknown outcomes, with explicit evidence provenance."""

from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelCallStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    request_id: UUID = Field(default_factory=uuid4)
    prompt: str = Field(min_length=1)


class ModelReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    request_id: UUID
    model_name: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ModelCallResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: UUID
    request_id: UUID
    model_name: str
    evidence_kind: Literal["simulation", "real_execution"] = "simulation"
    status: ModelCallStatus
    reply: ModelReply | None = None
    error_code: Literal["adapter_failure", "outcome_unknown", "invalid_response"] | None = None

    @model_validator(mode="after")
    def check_outcome(self) -> "ModelCallResult":
        if self.status == ModelCallStatus.SUCCEEDED:
            if self.reply is None or self.error_code is not None:
                raise ValueError("success requires a reply without an error")
            if (self.reply.task_id, self.reply.request_id, self.reply.model_name) != (
                self.task_id,
                self.request_id,
                self.model_name,
            ):
                raise ValueError("reply identity does not match the request and model")
        elif self.reply is not None:
            raise ValueError("unconfirmed or failed call cannot contain a confirmed reply")
        elif self.status == ModelCallStatus.UNKNOWN and self.error_code != "outcome_unknown":
            raise ValueError("UNKNOWN requires outcome_unknown")
        elif self.status == ModelCallStatus.FAILED and self.error_code not in {"adapter_failure", "invalid_response"}:
            raise ValueError("FAILED requires a confirmed failure reason")
        return self
