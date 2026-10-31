"""Explicit JSON settings with a small, allowlisted environment override set."""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from domain.tasks import RunLimits

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "examples/p2_01/config.fake.json"


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["fake"]
    model_name: Literal["fake-v1"]
    request_timeout_seconds: int = Field(gt=0, strict=True)
    max_output_tokens: int = Field(gt=0, strict=True)


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    model: ModelSettings
    run_limits: RunLimits


class ConfigurationError(ValueError):
    pass


ENV_OVERRIDES = {
    "ISSUE_AGENT_MAX_MODEL_CALLS": ("run_limits", "max_model_calls"),
    "ISSUE_AGENT_MODEL_TIMEOUT_SECONDS": ("model", "request_timeout_seconds"),
    "ISSUE_AGENT_MAX_OUTPUT_TOKENS": ("model", "max_output_tokens"),
}


def load_settings(path: Path, *, environ: Mapping[str, str] | None = None) -> AppSettings:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigurationError("configuration file cannot be read as JSON") from exc
    if not isinstance(data, dict):
        raise ConfigurationError("configuration must be a JSON object")
    env = os.environ if environ is None else environ
    unknown = {name for name in env if name.startswith("ISSUE_AGENT_")} - ENV_OVERRIDES.keys()
    if unknown:
        raise ConfigurationError(f"unsupported configuration variables: {', '.join(sorted(unknown))}")
    for name, (section, field) in ENV_OVERRIDES.items():
        if name in env:
            try:
                value = int(env[name])
            except ValueError as exc:
                raise ConfigurationError(f"{name} must contain an integer") from exc
            if not isinstance(data.get(section), dict):
                raise ConfigurationError(f"{section} must be a JSON object")
            data[section][field] = value
    try:
        return AppSettings.model_validate(data)
    except ValidationError as exc:
        details = [f"{'.'.join(map(str, error['loc']))}: {error['type']}" for error in exc.errors()]
        raise ConfigurationError("invalid configuration: " + "; ".join(details)) from exc
