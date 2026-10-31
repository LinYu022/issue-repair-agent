"""Persistent ownership and intent contracts; control epochs and lease epochs are separate."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkerIdentity(Contract):
    host_id: str
    boot_id: str
    pid: int = Field(gt=0, strict=True)
    start_ticks: int = Field(gt=0, strict=True)
    workspace_key: str


class Lease(Contract):
    task_id: UUID
    worker_id: UUID
    lease_epoch: int = Field(gt=0, strict=True)
    worker: WorkerIdentity


class CallIntent(Contract):
    task_id: UUID
    call_id: UUID
    kind: Literal["tool", "model", "snapshot", "export", "restore"]
    operation: str
    request: dict
    preconditions: dict
    execution_epoch: int = Field(ge=1, strict=True)
    retry_of: UUID | None = None
    possible_duplicate_billing_accepted: Literal[False] = False


class ExecutionRefused(ValueError):
    """Stable reason codes only; never include database connection details."""
