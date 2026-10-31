"""The current fake-tool request contract; no free-form command execution."""

from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from domain.models import ToolObservation


class FakeToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: Literal["success", "failure", "unknown"]


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    task_id: UUID
    call_id: UUID = Field(default_factory=uuid4)
    tool_name: Literal["fake_tool"] = "fake_tool"
    arguments: FakeToolArguments


def fake_tool_schema() -> dict:
    return {
        "schema_version": 1,
        "tool_name": "fake_tool",
        "request_schema": ToolRequest.model_json_schema(),
        "observation_schema": ToolObservation.model_json_schema(),
    }
