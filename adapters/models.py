"""A provider-neutral interface and a fake adapter; no SDK or network calls."""

from typing import Protocol

from domain.model_calls import ModelReply, ModelRequest
from runtime.config import ModelSettings


class AdapterFailure(Exception):
    """Adapter confirmed a failure, rather than an unconfirmed outcome."""


class AdapterOutcomeUnknown(Exception):
    """The adapter cannot confirm whether the request completed."""


class ModelAdapter(Protocol):
    model_name: str

    def generate(self, request: ModelRequest) -> ModelReply: ...


class FakeModelAdapter:
    def __init__(self, settings: ModelSettings, scenario: str):
        if scenario not in {"success", "failure", "unknown"}:
            raise ValueError("unsupported fake model scenario")
        self.model_name: str = settings.model_name
        self.settings = settings
        self.scenario = scenario
        self.calls = 0

    def generate(self, request: ModelRequest) -> ModelReply:
        self.calls += 1
        if self.scenario == "failure":
            raise AdapterFailure()
        if self.scenario == "unknown":
            raise AdapterOutcomeUnknown()
        return ModelReply(
            task_id=request.task_id,
            request_id=request.request_id,
            model_name=self.model_name,
            text="SIMULATION: inspect visible evidence before proposing a change.",
        )
