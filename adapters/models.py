"""Provider-neutral model interface and explicit failure outcomes."""

from typing import Protocol

from domain.model_calls import ModelReply, ModelRequest


class AdapterFailure(Exception):
    """Adapter confirmed a failure, rather than an unconfirmed outcome."""


class AdapterOutcomeUnknown(Exception):
    """The adapter cannot confirm whether the request completed."""


class ModelAdapter(Protocol):
    model_name: str

    def generate(self, request: ModelRequest) -> ModelReply: ...


