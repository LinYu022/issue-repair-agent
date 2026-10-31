"""Deterministic state context, complete artifacts and an explicitly lossy window control."""

import base64
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from domain.context import (
    ArtifactBrowseRequest,
    BrowseArtifact,
    ContextBuildRecord,
    ContextSettings,
    ObservationSummary,
    StructuredState,
    TranscriptEntry,
)
from domain.models import ToolObservation, ToolStatus
from domain.plans import document_hash, path_subset
from runtime.context_archive import ContextArchive
from runtime.diagnostic_evidence import environment_hash
from runtime.task_state import reserve_tool_call
from tools.readonly_workspace import WorkspaceRefused
from tools.workspace_files import sha

if TYPE_CHECKING:
    from runtime.repair_loop import RepairLoop


class MemoryProvider(Protocol):
    def retrieve(self, *, task_id: UUID, workspace_version: str) -> tuple[dict, ...]: ...
    def generation(self) -> int: ...


class NoMemory:
    def generation(self):
        return 0

    def retrieve(self, *, task_id: UUID, workspace_version: str) -> tuple[dict, ...]:
        return ()


class ContextManager:
    def __init__(self, loop: "RepairLoop", settings: ContextSettings):
        if settings.memory_enabled:
            raise WorkspaceRefused("semantic_memory_requires_explicit_host_binding")
        self.loop, self.session, self.settings = loop, loop.session, settings
        self.root = self.session.records.root / "context"
        self.archive = ContextArchive(
            self.root, self.session.records.artifacts, self.session.task.task_id, self.session.identity.workspace_id
        )
        self.root.mkdir(exist_ok=True)
        (self.root / "settings.json").write_text(settings.model_dump_json(indent=2) + "\n", encoding="utf-8")
        self.transcript: list[TranscriptEntry] = []
        self.observations: list[dict] = []
        self.events: list[dict] = []
        self.builds: list[ContextBuildRecord] = []
        self.build_attempts: list[dict] = []
        self.memory: MemoryProvider = NoMemory()
        self.initial_read_scope = loop.controller.discovery.paths
        self.pages: list[dict] = []

    def stamp(self):
        view = self.loop.controller.view()
        task = view["task"]
        return document_hash(
            {
                "task_id": task["task_id"],
                "version": task["workspace_version"],
                "grant_id": task["grant_id"],
                "revision_id": task["revision_id"],
                "epoch": task["execution_epoch"],
                "lifecycle": task["lifecycle"],
                "planning_events": view["planning_event_count"],
                "environment": self.session.task.environment_id,
                "image": self.session.runner.image,
                "resources": asdict(self.session.runner.limits),
                "profiles": {k: v.model_dump(mode="json") for k, v in self.session.profiles.items()},
                **({"memory_generation": self.memory.generation()} if self.settings.memory_enabled else {}),
            }
        )

    def message(self, role, text):
        source = self.archive.register(
            text,
            kind="model_proposal" if role == "assistant" else "feedback",
            workspace_version=self.session.task.workspace_version,
            provenance="model_unverified" if role == "assistant" else "controller_record",
        )
        entry = TranscriptEntry(
            sequence=len(self.transcript) + 1,
            role=role,
            source_id=source.source_id,
            workspace_version=source.workspace_version,
        )
        self.transcript.append(entry)
        directory = self.root / "transcript"
        directory.mkdir(exist_ok=True)
        (directory / f"{entry.sequence:04}.json").write_text(entry.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def event(self, event):
        if event["kind"] in {
            "strategy_decision",
            "replan_applied",
            "completion_claim_refused",
            "model_requested_input",
        }:
            source = self.archive.register(
                json.dumps(event, ensure_ascii=False),
                kind="controller_event",
                workspace_version=self.session.task.workspace_version,
                provenance="controller_record",
            )
            self.events.append({"source_id": str(source.source_id), "event": event})

    def tool(self, request, observation, payload):
        source = self.archive.register(
            json.dumps(payload, ensure_ascii=False),
            kind="tool_observation",
            workspace_version=request.expected_version,
            producer_call_id=request.call_id,
            path=getattr(request.arguments, "path", None),
            provenance="tool_observation",
        )
        related = []
        code = None
        if request.arguments.operation == "read" and observation.status == ToolStatus.SUCCEEDED:
            result = payload["result"]
            code = self.archive.register(
                result["text"],
                kind="source_code",
                workspace_version=request.expected_version,
                producer_call_id=request.call_id,
                path=request.arguments.path,
                source_file_sha256=result["sha256"],
                provenance="tool_observation",
            )
            related.append(code.source_id)
        streams = {}
        run = payload.get("run", {})
        for name in ("stdout", "stderr"):
            if name + "_base64" in run:
                raw = base64.b64decode(run[name + "_base64"], validate=True)
                reference = run["output_capture"][name]
                if sha(raw) != reference["sha256"] or len(raw) != reference["size_bytes"]:
                    raise ValueError("context_stream_integrity_mismatch")
                stream = self.archive.register(
                    raw.decode("utf-8", errors="replace"),
                    kind="process_" + name,
                    workspace_version=request.expected_version,
                    producer_call_id=request.call_id,
                    provenance="task_process_report",
                )
                related.append(stream.source_id)
                streams[name] = raw.decode("utf-8", errors="replace")
        result_text = json.dumps(payload.get("result", {}), ensure_ascii=False)
        excerpt = (streams.get("stderr", "") + "\n" + streams.get("stdout", "")).strip() if run else result_text
        self.observations.append(
            {
                "source": source,
                "request": request,
                "observation": observation,
                "payload": payload,
                "excerpt": excerpt,
                "related": tuple(related),
                "code": code,
            }
        )

    def investigation_progress(self):
        """Derive read coverage from existing observations; never grant edit authority."""
        current = self.session.task.workspace_version
        paths: dict[str, dict] = {}
        for item in self.observations:
            source = item["code"]
            if source is None or source.workspace_version != current:
                continue
            self.allowed(source)
            result = item["payload"]["result"]
            path = source.path
            entry = paths.setdefault(
                path, {"path": path, "ranges": [], "successful_reads": 0, "reads_without_new_lines": 0}
            )
            start, end = result["start_line"], result["end_line"]
            covered = any(left <= start and end <= right for left, right in entry["ranges"])
            entry["successful_reads"] += 1
            entry["reads_without_new_lines"] = entry["reads_without_new_lines"] + 1 if covered else 0
            merged: list[tuple[int, int]] = []
            for left, right in sorted([*entry["ranges"], (start, end)]):
                if merged and left <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], right))
                else:
                    merged.append((left, right))
            entry["ranges"] = merged
            entry["file_line_count"] = result["file_line_count"]
            read = self.loop.reads.get(path)
            entry["current_full_read_for_edit"] = bool(read and read[:2] == (current, source.source_file_sha256))
            entry["next_action"] = (
                {"operation": "tool", "arguments": {"operation": "read", "path": path}}
                if not entry["current_full_read_for_edit"]
                else None
            )
            entry["instruction"] = (
                "A full read is still required before editing. Use next_action with no line bounds; "
                "do not reread overlapping excerpts."
                if entry["next_action"]
                else "The full-read precondition is met. Investigate a different missing fact, revise the hypothesis, "
                "or make the supported in-scope change; do not reread unchanged code."
            )
            # Keep the most recently read paths without growing the prompt with the trace.
            paths.pop(path)
            paths[path] = entry
        return list(paths.values())[-3:]

    def allowed(self, source):
        controller = self.loop.controller
        if self.session.task.grant_id is None:
            raise WorkspaceRefused("context_grant_missing")
        grant = self.session.grants.get(self.session.task.grant_id)
        if grant is None:
            raise WorkspaceRefused("context_grant_missing")
        effective = controller.effective_grant(grant)
        if not path_subset(self.initial_read_scope, effective.paths):
            raise WorkspaceRefused("context_scope_narrowed_requires_new_session")
        if source.path and not path_subset((source.path,), effective.paths):
            raise WorkspaceRefused("context_source_outside_current_read_scope")
        return source

    def summary(self, item):
        source = self.allowed(item["source"])
        payload = item["payload"]
        observation = item["observation"]
        return ObservationSummary(
            source_id=source.source_id,
            call_id=source.producer_call_id,
            operation=item["request"].arguments.operation,
            profile_id=getattr(item["request"].arguments, "profile_id", None),
            input_version=source.workspace_version,
            output_version=payload.get("output_version", source.workspace_version),
            version_state="current_version"
            if source.workspace_version == self.session.task.workspace_version
            else "historical_version",
            status=observation.status.value,
            exit_code=observation.exit_code,
            reason=payload.get("reason")
            or payload.get("run", {}).get("reason")
            or payload.get("late_result_discarded"),
            self_test_status_at_observation=payload.get("self_test_status"),
            structured_test_metadata=next(
                (
                    f.metadata.model_dump(mode="json")
                    for f in self.loop.replanner.facts
                    if f.call_id == source.producer_call_id and f.metadata is not None
                ),
                None,
            ),
            excerpt=item["excerpt"][: self.settings.excerpt_chars],
            excerpt_truncated=len(item["excerpt"]) > self.settings.excerpt_chars,
            related_sources=item["related"],
        )

    def state(self):
        view = self.loop.controller.view()
        task = view["task"]
        current = self.session.task.workspace_version
        if current is None:
            raise WorkspaceRefused("context_version_missing")
        active, refuted, historical = [], [], []
        ledger = self.loop.replanner.ledger
        for path in sorted(ledger.root.glob("*")):
            chain = ledger._history(UUID(path.name))
            if not chain:
                continue
            latest = chain[-1]
            for revision in chain:
                ledger._validate(revision)
                if revision.status == "refuted" and revision.revision_id != latest.revision_id:
                    source = self.archive.register(
                        revision.model_dump_json(),
                        kind="hypothesis_revision",
                        workspace_version=revision.subject_version,
                        provenance="hypothesis_not_proven",
                    )
                    refuted.append(
                        {
                            "hypothesis_id": str(revision.hypothesis_id),
                            "revision_id": str(revision.revision_id),
                            "claim": revision.claim,
                            "status": "refuted",
                            "subject_version": revision.subject_version,
                            "version_state": "current_version"
                            if revision.subject_version == current
                            else "historical_version",
                            "opposing_evidence_ids": [str(i) for i in revision.opposing_evidence_ids],
                            "source_id": str(source.source_id),
                            "historical_revision_preserved": True,
                        }
                    )
            source = self.archive.register(
                latest.model_dump_json(),
                kind="hypothesis_revision",
                workspace_version=latest.subject_version,
                provenance="hypothesis_not_proven",
            )
            version_state = "current_version" if latest.subject_version == current else "historical_version"
            item = {
                "hypothesis_id": str(latest.hypothesis_id),
                "claim": latest.claim,
                "status": latest.status,
                "confidence": latest.confidence,
                "subject_version": latest.subject_version,
                "subject_environment_hash": latest.subject_environment_hash,
                "version_state": version_state,
                "source_id": str(source.source_id),
                "supporting_evidence_ids": [str(i) for i in latest.supporting_evidence_ids],
                "opposing_evidence_ids": [str(i) for i in latest.opposing_evidence_ids],
                "next_check": latest.next_check.model_dump(mode="json"),
            }
            if latest.status in {"refuted", "superseded"}:
                refuted.append(item)
            elif latest.subject_version == current and any(
                environment_hash(f.context) == latest.subject_environment_hash
                for f in self.loop.replanner.facts
                if f.workspace_version == current
            ):
                active.append(item)
            else:
                historical.append(item)
        failures = [
            self.summary(i)
            for i in self.observations
            if i["observation"].status != ToolStatus.SUCCEEDED or i["payload"].get("late_result_discarded")
        ]
        recent = [self.summary(i) for i in self.observations[-self.settings.recent_observations :]]
        code: list[dict] = []
        code_budget = min(self.settings.max_context_bytes, self.loop.registration.max_context_bytes) // 2
        for item in reversed(self.observations):
            source = item["code"]
            if source is None or any(c["path"] == source.path for c in code):
                continue
            self.allowed(source)
            text = self.archive.artifacts.read_text(source.artifact)
            excerpt = text if len(text.encode()) <= code_budget else text[: self.settings.excerpt_chars]
            code_budget = max(0, code_budget - len(excerpt.encode()))
            result = item["payload"]["result"]
            full_read = item["request"].arguments.start_line is None
            edit_read = self.loop.reads.get(source.path)
            code.append(
                {
                    "path": source.path,
                    "file_sha256": source.source_file_sha256,
                    "source_id": str(source.source_id),
                    "workspace_version": source.workspace_version,
                    "version_state": "current_version" if source.workspace_version == current else "historical_version",
                    "read_scope": "full_file" if full_read else "line_range",
                    "start_line": result["start_line"],
                    "end_line": result["end_line"],
                    "file_line_count": result["file_line_count"],
                    "current_full_read_for_edit": bool(
                        edit_read and edit_read[:2] == (current, source.source_file_sha256)
                    ),
                    "excerpt": excerpt,
                    "excerpt_truncated": len(text) > len(excerpt),
                    "total_characters": len(text),
                    "next_page": {
                        "operation": "browse_artifact",
                        "source_id": str(source.source_id),
                        "offset": len(excerpt),
                        "limit": 4096,
                    }
                    if len(text) > len(excerpt)
                    else None,
                    "instruction": (
                        "This source contains the recorded full file. Use next_page if truncated; historical text is not current code."
                        if full_read
                        else "This source contains only the stated line range. Its pages do not contain omitted lines. A current full read with no line bounds is required before editing."
                    ),
                }
            )
            if len(code) >= 3:
                break
        notes = []
        for event in self.loop.controller.records.events:
            if event["kind"] == "human_diagnostic_hint":
                text = self.loop.controller.records.artifacts.read_text(
                    __import__("domain.models", fromlist=["ArtifactRef"]).ArtifactRef.model_validate(
                        event["payload_artifact"]
                    )
                )
                notes.append(
                    {
                        "event_id": event["event_id"],
                        "provenance": "operator_hint_not_authority",
                        "payload": json.loads(text),
                    }
                )
        return StructuredState(
            task_id=self.session.task.task_id,
            current_workspace_version=current,
            current_plan=view["plan"],
            authorization={
                "mode": view["mode"],
                "authorization": view["authorization"],
                "authorization_active": view["authorization_active"],
                "grant_id": task["grant_id"],
                "revision_id": task["revision_id"],
                "execution_epoch": task["execution_epoch"],
            },
            task_state={
                **{
                    key: task[key]
                    for key in (
                        "lifecycle",
                        "self_test_status",
                        "limits",
                        "tool_calls_reserved",
                        "model_calls_reserved",
                    )
                },
                "replanning": self.loop.replanner.feedback(),
                "completion": self.loop._completion(),
                "validation_profiles": {
                    name: {
                        "state": "pending"
                        if name not in self.loop.tests
                        else "stale"
                        if self.loop.tests[name]["workspace_version"] != current
                        else "passed"
                        if self.loop.tests[name]["passed"]
                        else "failed",
                        "evidence": self.loop.tests.get(name),
                    }
                    for name in self.loop.registration.required_profiles
                },
            },
            active_hypotheses=tuple(active),
            refuted_or_superseded_hypotheses=tuple(refuted),
            historical_hypotheses=tuple(historical),
            failures_and_counterevidence=tuple(failures),
            recent_observations=tuple(recent),
            key_code=tuple(code),
            strategy_changes=tuple(self.events),
            classifications=tuple(
                self.loop.replanner.ledger.read_classification(UUID(p.stem)).model_dump(mode="json")
                for p in sorted((ledger.root.parent / "classifications").glob("*.json"))
            ),
            operator_notes=tuple(notes),
            expanded_pages=tuple(
                {
                    **page,
                    "version_state": "current_version"
                    if page["source"]["workspace_version"] == current
                    else "historical_version",
                }
                for page in self.pages[-2:]
            ),
            memory={
                "enabled": self.settings.memory_enabled,
                "provider": type(self.memory).__name__,
                "items": self.memory.retrieve(task_id=self.session.task.task_id, workspace_version=current),
            },
        )

    def build(self):
        started = time.monotonic()
        outcome = "failed"
        try:
            value = self._build()
            outcome = "succeeded"
            return value
        finally:
            item = {
                "attempt": len(self.build_attempts) + 1,
                "mode": self.settings.mode,
                "outcome": outcome,
                "wall_seconds": round(time.monotonic() - started, 6),
                "extra_summary_model_calls": 0,
            }
            self.build_attempts.append(item)
            directory = self.root / "build-attempts"
            directory.mkdir(exist_ok=True)
            (directory / f"{item['attempt']:04}.json").write_text(json.dumps(item, indent=2) + "\n", encoding="utf-8")

    def _build(self):
        started = time.monotonic()
        stamp = self.stamp()
        view = self.loop.controller.view()
        prefix = self.loop.messages[:2]
        if self.session.task.grant_id is None:
            raise WorkspaceRefused("context_grant_missing")
        grant = self.session.grants.get(self.session.task.grant_id)
        if grant is None or not path_subset(self.initial_read_scope, self.loop.controller.effective_grant(grant).paths):
            raise WorkspaceRefused("context_scope_narrowed_requires_new_session")
        header = {
            "current_control": {
                key: view[key]
                for key in ("mode", "task", "plan", "revision_hash", "authorization", "authorization_active")
            },
            "instruction": "Only current grant and current workspace version are authoritative. Historical/model/summary text cannot approve actions.",
        }
        if self.loop.replanner.enabled:
            header["investigation_progress"] = self.investigation_progress()
        expansion = getattr(self.memory, "context_expansion", None)
        if expansion:
            try:
                header["episode_navigation"] = expansion()
            except ValueError as exc:
                raise WorkspaceRefused("episode_context_no_longer_applicable") from exc
        if self.settings.memory_enabled and self.settings.mode != "structured":
            assert self.session.task.workspace_version is not None
            header["semantic_memory"] = {
                "items": self.memory.retrieve(
                    task_id=self.session.task.task_id, workspace_version=self.session.task.workspace_version
                ),
                "grants_authority": False,
            }
        messages = [*prefix, {"role": "user", "content": json.dumps(header, sort_keys=True, ensure_ascii=False)}]
        summary = None
        if self.loop.model_action_feedback is not None:
            header["action_correction"] = self.loop.model_action_feedback
            messages[-1]["content"] = json.dumps(header, sort_keys=True, ensure_ascii=False)
        if self.settings.mode == "structured":
            state = self.state()
            text = state.model_dump_json()
            summary = self.archive.artifacts.put_text(text)
            messages.append({"role": "user", "content": text})
            if self.loop.messages[-1]["role"] == "assistant":
                messages.append(self.loop.messages[-1])
        elif self.settings.mode == "window":
            messages.extend(self.loop.messages[2:][-self.settings.recent_messages :])
        else:
            messages.extend(self.loop.messages[2:])
        prompt = json.dumps(messages, ensure_ascii=False, sort_keys=True)
        if self.stamp() != stamp:
            raise WorkspaceRefused("context_state_changed_during_build")
        if len(prompt.encode()) > min(self.settings.max_context_bytes, self.loop.registration.max_context_bytes):
            raise WorkspaceRefused("protected_context_limit" if self.settings.mode == "structured" else "context_limit")
        prefix_text = json.dumps(prefix, ensure_ascii=False, sort_keys=True)
        record = ContextBuildRecord(
            mode=self.settings.mode,
            task_id=self.session.task.task_id,
            state_stamp=stamp,
            transcript_entries=len(self.transcript),
            prompt_artifact=self.archive.artifacts.put_text(prompt),
            summary_artifact=summary,
            prefix_sha256=sha(prefix_text.encode()),
            prefix_utf8_bytes=len(prefix_text.encode()),
            prompt_utf8_bytes=len(prompt.encode()),
            prompt_characters=len(prompt),
            preparation_wall_seconds=round(time.monotonic() - started, 6),
        )
        directory = self.root / "builds"
        directory.mkdir(exist_ok=True)
        (directory / f"{record.build_id}.json").write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        self.builds.append(record)
        return prompt, stamp

    def browse(self, arguments: BrowseArtifact):
        durable = self.session.durable
        if durable is not None:
            from uuid import uuid4

            call_id = uuid4()
            intent = durable.intent("tool", call_id, "artifact_browse", arguments.model_dump(mode="json"))
            with durable.channel(intent) as previous:
                if previous is not None:
                    return previous["result"]
                value = self._browse(arguments, call_id)
                durable.finish(intent, value["status"], value)
                return value
        return self._browse(arguments)

    def _browse(self, arguments: BrowseArtifact, call_id=None):
        session = self.session
        if not session.lock.acquire(blocking=False):
            raise WorkspaceRefused("artifact_browse_while_tool_active")
        try:
            sentinel = self.loop._request({"operation": "diff", "path": "."})
            reason = session.stop_reason(sentinel)
            if reason:
                raise WorkspaceRefused(reason)
            self.loop.controller.check_budget(sentinel)
            source = self.allowed(self.archive.source(arguments.source_id))
            page = self.archive.page(
                source.source_id,
                current_version=sentinel.expected_version,
                offset=arguments.offset,
                limit=arguments.limit,
            )
            if (
                len(json.dumps(page.model_dump(mode="json"), ensure_ascii=False).encode())
                > self.loop.controller.effective_grant(session.grants[sentinel.grant_id]).max_output_bytes
            ):
                raise WorkspaceRefused("tool_output_limit")
            session.task = reserve_tool_call(
                session.task,
                elapsed_seconds=time.monotonic() - session.started,
                readonly_nonrunning=self.loop.controller.allows_nonrunning(sentinel),
            )
            request = ArtifactBrowseRequest(
                task_id=sentinel.task_id,
                workspace_id=sentinel.workspace_id,
                grant_id=sentinel.grant_id,
                revision_id=sentinel.revision_id,
                execution_epoch=sentinel.execution_epoch,
                expected_version=sentinel.expected_version,
                arguments=arguments,
                **({"call_id": call_id} if call_id is not None else {}),
            )
            late = session.stop_reason(sentinel)
            output = {
                "source_id": str(source.source_id),
                "page": None if late else page.model_dump(mode="json"),
                "reason": late,
                "input_version": sentinel.expected_version,
                "output_version": session.task.workspace_version,
            }
            observation = ToolObservation(
                task_id=request.task_id,
                call_id=request.call_id,
                tool_name="artifact_browse",
                evidence_kind="real_execution",
                status=ToolStatus.FAILED if late else ToolStatus.SUCCEEDED,
                exit_code=125 if late else 0,
                recorded_at=datetime.now(timezone.utc),
                artifact=session.records.artifacts.put_text(json.dumps(output, ensure_ascii=False)),
            )
            session.records.save(session.task, request, observation)
            self.loop.controller.after_call(request, observation)
            self.loop._event(
                "tool_observation",
                call_id=str(request.call_id),
                operation="artifact_browse",
                input_version=request.expected_version,
                output_version=session.task.workspace_version,
                revision_id=str(request.revision_id),
                grant_id=str(request.grant_id),
                execution_epoch=request.execution_epoch,
                status=observation.status.value,
                artifact=observation.artifact.model_dump(mode="json"),
            )
            if not late:
                self.pages.append(page.model_dump(mode="json"))
            return {"status": observation.status.value, "call_id": str(request.call_id), "evidence": output}
        finally:
            session.lock.release()

    def report(self):
        return {
            "mode": self.settings.mode,
            "builds": len(self.builds),
            "prompt_utf8_bytes_total": sum(b.prompt_utf8_bytes for b in self.builds),
            "preparation_wall_seconds_total": sum(b["wall_seconds"] for b in self.build_attempts),
            "build_attempts": len(self.build_attempts),
            "failed_build_attempts": sum(b["outcome"] == "failed" for b in self.build_attempts),
            "extra_summary_model_calls": 0,
            "extra_summary_model_tokens": 0,
            "memory_enabled": self.settings.memory_enabled,
            "semantic_retrievals": getattr(self.memory, "retrievals", 0),
            "semantic_items_returned": getattr(self.memory, "items_returned", 0),
            "episode_recalls": getattr(self.memory, "episode_recalls", 0),
            "episode_reads": getattr(self.memory, "episode_reads", 0),
            "prefix_cache_interpretation": "Stable prefix identity recorded; provider cache hits require service usage evidence, never extra context capacity.",
        }
