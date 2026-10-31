"""Evidence/hypothesis records separate from execution state and hidden evaluator data."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from domain.models import ArtifactRef, ToolObservation
from domain.repository_evidence import LocationHypothesis, RepositoryObservation, SourceExcerpt
from domain.workspace_tools import WorkspaceToolRequest
from runtime.artifacts import LocalArtifactStore
from runtime.call_records import CallRecordStore
from tools.source_spans import physical_lines


class InvestigationRecords:
    def __init__(self, root: Path, artifacts: LocalArtifactStore):
        self.root, self.artifacts = root, artifacts

    @staticmethod
    def items(operation: str, result: dict):
        if operation == "read":
            return [("source_excerpt", "workspace_text", {"excerpt": result.get("excerpt")})]
        if operation == "search":
            return [("source_excerpt", "workspace_text", item) for item in result.get("matches", [])]
        if operation == "symbols":
            kind = "static_definition" if result["mode"] == "definitions" else "static_reference"
            return [(kind, "python_ast", item) for item in result["hits"]]
        if operation == "investigate":
            return [("reported_location", "reported_input", item) for item in result["candidates"]]
        return []

    def capture(
        self, request: WorkspaceToolRequest, result: dict, version: str, files: dict[str, str], evidence: ArtifactRef
    ):
        observations = []
        self.artifacts.read_text(evidence)
        for kind, source, item in self.items(request.arguments.operation, result):
            if item.get("excerpt") is None:
                continue
            quote = SourceExcerpt.model_validate(item["excerpt"])
            if quote.path not in files:
                raise ValueError("observation_source_not_visible")
            import hashlib

            if hashlib.sha256(files[quote.path].encode()).hexdigest() != quote.file_sha256:
                raise ValueError("observation_source_hash_mismatch")
            lines = physical_lines(files[quote.path])
            if "".join(lines[quote.start_line - 1 : quote.end_line]) != quote.text:
                raise ValueError("observation_source_span_mismatch")
            data = {
                "task_id": request.task_id,
                "workspace_id": request.workspace_id,
                "workspace_version": version,
                "call_id": request.call_id,
                "operation": request.arguments.operation,
                "kind": kind,
                "source": source,
                "excerpt": quote,
                "symbol": item.get("qualified_name") or item.get("symbol") or item.get("target"),
                "static_resolution": item.get("resolution"),
                "recorded_at": datetime.now(timezone.utc),
                "evidence_artifact": evidence,
            }
            observation = RepositoryObservation.model_validate(data)
            path = self.root / "observations" / f"{observation.observation_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(observation.model_dump_json(indent=2) + "\n")
            observations.append(observation)
        return observations

    def read(self, observation_id: UUID, *, task_id: UUID, workspace_id: UUID, current_version: str):
        observation = RepositoryObservation.model_validate_json(
            (self.root / "observations" / f"{observation_id}.json").read_text(encoding="utf-8")
        )
        if (observation.task_id, observation.workspace_id) != (task_id, workspace_id):
            raise ValueError("observation_identity_mismatch")
        trace = CallRecordStore(self.root.parent)
        record, request, result = trace.read(self.root.parent / "calls/tool" / f"{observation.call_id}.json")
        if not isinstance(request, WorkspaceToolRequest) or not isinstance(result, ToolObservation):
            raise ValueError("observation_requires_workspace_tool_call")
        output = json.loads(self.artifacts.read_text(result.artifact))
        if (
            record.task_id != task_id
            or record.workspace_version != observation.workspace_version
            or getattr(request, "workspace_id", None) != workspace_id
            or str(observation_id) not in output.get("observation_ids", [])
            or result.status.value != "SUCCEEDED"
        ):
            raise ValueError("observation_call_link_mismatch")
        raw = json.loads(self.artifacts.read_text(observation.evidence_artifact))
        if (
            raw["workspace_version"] != observation.workspace_version
            or raw["call_id"] != str(observation.call_id)
            or raw["operation"] != observation.operation
            or raw["result"] != output.get("result")
        ):
            raise ValueError("observation_evidence_identity_mismatch")
        matching = []
        for kind, source, item in self.items(observation.operation, raw["result"]):
            if (
                item.get("excerpt") == observation.excerpt.model_dump(mode="json")
                and kind == observation.kind
                and source == observation.source
            ):
                matching.append(item)
        if not matching or not any(
            (item.get("qualified_name") or item.get("symbol") or item.get("target")) == observation.symbol
            and item.get("resolution") == observation.static_resolution
            for item in matching
        ):
            raise ValueError("observation_does_not_match_source_artifact")
        return observation, "current" if current_version == observation.workspace_version else "historical_stale"

    def propose(
        self, *, task_id: UUID, workspace_id: UUID, workspace_version: str, claim: str, support: tuple[UUID, ...]
    ):
        for identity in support:
            _observation, state = self.read(
                identity, task_id=task_id, workspace_id=workspace_id, current_version=workspace_version
            )
            if state != "current":
                raise ValueError("stale_observation_cannot_support_new_hypothesis")
        hypothesis = LocationHypothesis(
            task_id=task_id,
            workspace_id=workspace_id,
            workspace_version=workspace_version,
            claim=claim,
            supporting_observation_ids=support,
            created_at=datetime.now(timezone.utc),
        )
        path = self.root / "hypotheses" / f"{hypothesis.hypothesis_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(hypothesis.model_dump_json(indent=2) + "\n")
        return hypothesis

    def read_hypothesis(self, hypothesis_id: UUID, *, task_id: UUID, workspace_id: UUID, current_version: str):
        hypothesis = LocationHypothesis.model_validate_json(
            (self.root / "hypotheses" / f"{hypothesis_id}.json").read_text(encoding="utf-8")
        )
        if (hypothesis.task_id, hypothesis.workspace_id) != (task_id, workspace_id):
            raise ValueError("hypothesis_identity_mismatch")
        for identity in hypothesis.supporting_observation_ids:
            _record, state = self.read(
                identity, task_id=task_id, workspace_id=workspace_id, current_version=hypothesis.workspace_version
            )
            if state != "current":
                raise ValueError("hypothesis_support_version_mismatch")
        return (
            hypothesis,
            "current_unverified" if current_version == hypothesis.workspace_version else "historical_stale_unverified",
        )
