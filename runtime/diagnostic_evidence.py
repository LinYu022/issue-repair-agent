"""Normalize already completed calls; raw process bytes remain independently checked."""

import base64
import json
from pathlib import Path
from typing import Literal
from uuid import UUID

from domain.failure_diagnosis import (
    DiagnosticEvidence,
    DiagnosticProfile,
    ExecutionContext,
    ModelAnalysis,
    TestMetadata,
)
from domain.model_calls import ModelCallResult
from domain.models import ToolObservation
from domain.plans import document_hash
from domain.workspace_tools import WorkspaceToolRequest
from runtime.call_records import CallRecordStore
from tools.workspace_files import sha


def environment_hash(context: ExecutionContext | None) -> str:
    if context is None:
        return document_hash({"context": "unknown"})
    # Ledger validity includes the test contract too. Classifier contrasts also
    # independently require identical logical/profile hashes before comparing.
    return document_hash(context.model_dump())


class DiagnosticEvidenceStore:
    def __init__(self, trace: CallRecordStore):
        self.trace = trace
        self.root = trace.root / "diagnosis"

    def capture(self, call_id: UUID, profile: DiagnosticProfile | None = None) -> DiagnosticEvidence:
        record, request, result = self.trace.read(self.trace.root / "calls/tool" / f"{call_id}.json")
        if not isinstance(request, WorkspaceToolRequest) or not isinstance(result, ToolObservation):
            raise ValueError("diagnostic_evidence_requires_workspace_tool")
        output = json.loads(self.trace.artifacts.read_text(result.artifact))
        if output["input_version"] != request.expected_version:
            raise ValueError("diagnostic_input_version_mismatch")
        context = (
            ExecutionContext.model_validate(output["execution_context"]) if "execution_context" in output else None
        )
        metadata = None
        state: Literal["not_requested", "valid", "invalid", "legacy_context_missing"] = (
            "not_requested" if context else "legacy_context_missing"
        )
        run = output.get("run", {})
        if request.arguments.operation == "run_test" and profile and profile.profile_id != request.arguments.profile_id:
            raise ValueError("diagnostic_profile_id_mismatch")
        if context and profile and profile.profile_hash != context.profile_hash:
            raise ValueError("diagnostic_profile_hash_mismatch")
        # A confirmed call record can still have an UNKNOWN dispatch outcome
        # before a process was recorded. Keep that uncertainty; invent no run.
        if request.arguments.operation == "run_test" and (run or result.status.value != "UNKNOWN"):
            process = json.loads((self.trace.root / "processes" / f"{call_id}.json").read_text(encoding="utf-8"))
            if (process["call_id"], process["task_id"], process["profile_id"], process["execution_epoch"]) != (
                str(call_id),
                str(request.task_id),
                request.arguments.profile_id,
                request.execution_epoch,
            ):
                raise ValueError("diagnostic_process_identity_mismatch")
            if not process["cleanup_confirmed"] or process["output_capture"] != run.get("output_capture"):
                raise ValueError("diagnostic_process_not_confirmed")
            if any(
                process.get(key) != run.get(key)
                for key in (
                    "task_id",
                    "call_id",
                    "execution_epoch",
                    "image_id",
                    "profile_id",
                    "exit_code",
                    "reason",
                    "cleanup_confirmed",
                )
            ):
                raise ValueError("diagnostic_process_result_mismatch")
            if context and (
                context.image_id != process["image_id"] or profile and profile.profile_hash != context.profile_hash
            ):
                raise ValueError("diagnostic_runtime_context_mismatch")
            raw = {}
            for name in ("stdout", "stderr"):
                data = (self.trace.root / "processes" / f"{call_id}.{name}.bin").read_bytes()
                info = process["output_capture"][name]
                if sha(data) != info["sha256"] or len(data) != info["size_bytes"]:
                    raise ValueError("diagnostic_raw_stream_integrity")
                if base64.b64decode(run[name + "_base64"], validate=True) != data:
                    raise ValueError("diagnostic_raw_stream_result_mismatch")
                raw[name] = data
            if context and profile and profile.metadata_format == "fixture-json-v1":
                try:
                    metadata = TestMetadata.model_validate_json(raw["stdout"])
                    if (
                        metadata.check_id != profile.logical_check_id
                        or (metadata.outcome == "passed") != (run.get("exit_code") == 0)
                        or run.get("reason")
                        or output.get("late_result_discarded")
                    ):
                        raise ValueError("metadata_not_coherent_with_process")
                    state = "valid"
                except ValueError:
                    metadata, state = None, "invalid"
        evidence = DiagnosticEvidence(
            task_id=request.task_id,
            workspace_id=request.workspace_id,
            workspace_version=request.expected_version,
            call_id=call_id,
            operation=request.arguments.operation,
            status=result.status.value,
            exit_code=result.exit_code,
            source_artifact=result.artifact,
            context=context,
            profile=profile,
            metadata=metadata,
            metadata_state=state,
            runner_reason=run.get("reason") or output.get("reason"),
            late_result=bool(output.get("late_result_discarded")),
            recorded_at=record.recorded_at,
            evidence_kind=record.evidence_kind,
        )
        directory = self.root / "evidence"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{evidence.evidence_id}.json").write_text(
            evidence.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        return evidence

    def read(self, evidence_id: UUID) -> DiagnosticEvidence:
        saved = DiagnosticEvidence.model_validate_json(
            (self.root / "evidence" / f"{evidence_id}.json").read_text(encoding="utf-8")
        )
        # Re-normalize without writing a second observation, checking all original
        # links and process streams using the same capture validation path.
        import tempfile

        with tempfile.TemporaryDirectory(prefix="verify-diagnostic-") as directory:
            verifier = DiagnosticEvidenceStore(self.trace)
            verifier.root = Path(directory)
            actual = verifier.capture(saved.call_id, saved.profile)
        if actual.model_dump(exclude={"evidence_id"}) != saved.model_dump(exclude={"evidence_id"}):
            raise ValueError("diagnostic_evidence_artifact_mismatch")
        return saved

    def capture_model_plans(self, call_id: UUID) -> tuple[ModelAnalysis, ...]:
        record, _request, result = self.trace.read(self.trace.root / "calls/model" / f"{call_id}.json")
        if not isinstance(result, ModelCallResult) or result.reply is None:
            return ()
        value = json.loads(result.reply.text)
        if value.get("operation") != "plan" or not isinstance(value.get("draft", {}).get("hypotheses"), list):
            return ()
        claims = []
        for text in value["draft"]["hypotheses"][:16]:
            claim = ModelAnalysis(
                task_id=record.task_id,
                call_id=call_id,
                workspace_version=record.workspace_version or "unknown",
                claim=text,
                source_artifact=record.result_artifact,
                evidence_kind=record.evidence_kind,
            )
            target = self.root / "model-analyses"
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{claim.analysis_id}.json").write_text(claim.model_dump_json(indent=2) + "\n", encoding="utf-8")
            claims.append(claim)
        return tuple(claims)

    def read_model_analysis(self, analysis_id: UUID) -> ModelAnalysis:
        claim = ModelAnalysis.model_validate_json(
            (self.root / "model-analyses" / f"{analysis_id}.json").read_text(encoding="utf-8")
        )
        record, _request, result = self.trace.read(self.trace.root / "calls/model" / f"{claim.call_id}.json")
        if (
            not isinstance(result, ModelCallResult)
            or result.reply is None
            or claim.claim not in json.loads(result.reply.text).get("draft", {}).get("hypotheses", [])
            or claim.source_artifact != record.result_artifact
            or claim.task_id != record.task_id
            or claim.workspace_version != (record.workspace_version or "unknown")
            or claim.evidence_kind != record.evidence_kind
        ):
            raise ValueError("model_analysis_source_mismatch")
        return claim
