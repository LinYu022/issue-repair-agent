"""Independent visible checks on immutable closed-source copies; no hidden evaluator labels."""

import json
from contextlib import ExitStack
from uuid import UUID, uuid4

from domain.plans import document_hash
from domain.workspace_tools import WorkspaceToolRequest
from runtime.container_runner import ContainerRunner
from runtime.episode_ingest import EpisodeIngestor
from runtime.execution_guard import ExclusiveFile
from tools.workspace_files import sha, workspace_version


class VisibleSkillVerifier:
    def __init__(self, store, *, suite_id, profiles, records):
        # Host supplies visible profiles; no API/Agent request may replace them.
        self.store, self.suite_id, self.profiles, self.records = store, suite_id, dict(profiles), records

    def disclosure(self):
        return {
            "suite_id": self.suite_id,
            "visibility": "exposed_development",
            "profiles": {name: p.model_dump(mode="json") for name, p in self.profiles.items()},
        }

    def check_disclosure(self, loop):
        expected = self.disclosure()
        entries = {entry.path: entry for entry in loop.package.files}
        entry = entries.get("skill-visible-validation.json")
        if entry is None or json.loads(entry.text) != expected:
            raise ValueError("skill_full_visible_validation_disclosure_required")
        current = loop.session.files.snapshot().get("skill-visible-validation.json")
        if current != entry.text:
            raise ValueError("skill_visible_validation_disclosure_changed")
        return document_hash(expected)

    def complete(self, loop, user, branch, attempt_id):
        self.check_disclosure(loop)
        selection = getattr(loop.context_manager.memory, "skill_selection", None)
        if not selection or UUID(selection["attempt_id"]) != attempt_id:
            raise ValueError("skill_verifier_requires_actual_selected_plan")
        current = loop.controller.current
        if current is None:
            raise ValueError("skill_current_plan_required")
        if current.body.model_dump(mode="json", include=set(selection["draft"])) != selection["draft"]:
            raise ValueError("skill_selected_draft_not_current_plan")
        result = EpisodeIngestor(self.store.episodes).capture(loop, user, branch)
        with self.store.semantic.transaction(user) as (c, _):
            _, episode = self.store.episodes._entry(c, user, UUID(result["episode_id"]))
            attempt = c.execute(
                "SELECT * FROM public.agent_skill_attempts WHERE user_id=%s AND attempt_id=%s", (user, attempt_id)
            ).fetchone()
            if not attempt or document_hash(attempt["proposal"]) != attempt["proposal_hash"]:
                raise ValueError("skill_attempt_integrity")
            declaration = c.execute(
                "SELECT * FROM public.agent_skill_validation_tasks WHERE user_id=%s AND task_id=%s",
                (user, loop.session.task.task_id),
            ).fetchone()
            if attempt["mode"] == "validation" and (
                declaration is None
                or document_hash(declaration["declaration"]) != declaration["declaration_hash"]
                or declaration["declaration"]["suite"] != self.suite_id
            ):
                raise ValueError("skill_verifier_visible_registration_required")
        return self._verify_closed(loop, user, episode, attempt_id, attempt)

    def _verify_closed(self, loop, user, episode, attempt_id, attempt):
        owner = loop.session.durable
        with loop.state_lock, loop.session.lock, ExitStack() as stack:
            for path in [owner.lock_path, owner.lock_path.with_suffix(".channel")]:
                lock = ExclusiveFile(path)
                stack.callback(lock.close)
            owner.require_no_containers()
            row = owner.store.read(loop.session.task.task_id)
            if (
                not loop.closed
                or not owner.closed
                or not row["quiescent"]
                or row["lease_epoch"] != owner.lease.lease_epoch
            ):
                raise ValueError("skill_verification_requires_quiescent_closed_source")
            files = loop.session.files.snapshot()
            disclosure_hash = self.check_disclosure(loop)
            if workspace_version(files) != episode.source_final_version:
                raise ValueError("skill_verification_workspace_changed")
            actions = attempt["proposal"]["actions"]
            proof = []
            sequence = []
            for event in loop.events:
                if event["kind"] != "tool_observation":
                    continue
                record, request, result = loop.session.records.read(
                    loop.session.records.root / "calls/tool" / f"{event['call_id']}.json"
                )
                if not isinstance(request, WorkspaceToolRequest):
                    continue
                if record.recorded_at < attempt["created_at"]:
                    continue
                expected = request.arguments.model_dump(mode="json")
                normalized = {"operation": expected["operation"]}
                if expected["operation"] == "read":
                    normalized["path"] = expected["path"]
                    if expected.get("start_line") is not None:
                        continue
                elif expected["operation"] == "run_test":
                    normalized["profile_id"] = expected["profile_id"]
                else:
                    continue
                ledger = owner.store.call(loop.session.task.task_id, request.call_id)
                if (
                    ledger is None
                    or ledger["state"] != record.status
                    or ledger["intent"]["request"] != request.model_dump(mode="json")
                ):
                    raise ValueError("skill_original_ledger_mismatch")
                if result.status.value == "SUCCEEDED" and request.expected_version == episode.source_final_version:
                    sequence.append(normalized)
                    proof.append(
                        {"call_id": str(request.call_id), "record_hash": document_hash(record), "arguments": normalized}
                    )
            expected_at = 0
            for action in sequence:
                if expected_at < len(actions) and action == actions[expected_at]:
                    expected_at += 1
            procedure_ok = (
                expected_at == len(actions) and episode.source_final_version == attempt["proposal"]["workspace_version"]
            )
            tests = []
            runner = ContainerRunner(
                loop.session.runner.image, self.records / str(attempt_id), loop.session.runner.limits
            )
            for profile in self.profiles.values():
                call_id = uuid4()
                observation = runner.run(
                    loop.session.task.task_id,
                    call_id,
                    0,
                    files,
                    profile,
                    max_seconds=20,
                    max_output_bytes=65536,
                    should_stop=lambda: None,
                )
                process = json.loads((runner.records / f"{call_id}.json").read_text())
                for stream, info in process["output_capture"].items():
                    raw = (runner.records / f"{call_id}.{stream}.bin").read_bytes()
                    if len(raw) != info["size_bytes"] or sha(raw) != info["sha256"]:
                        raise ValueError("skill_independent_stream_integrity")
                tests.append(
                    {
                        "call_id": str(call_id),
                        "profile_id": profile.profile_id,
                        "profile_hash": document_hash(profile),
                        "process_hash": document_hash(process),
                        "exit_code": observation["exit_code"],
                        "cleanup_confirmed": observation["cleanup_confirmed"],
                        "reason": observation["reason"],
                    }
                )
            evidence = {
                "task_id": str(loop.session.task.task_id),
                "episode_id": str(episode.episode_id),
                "workspace_version": episode.source_final_version,
                "suite_id": self.suite_id,
                "visibility": "exposed_development",
                "disclosed_suite_hash": disclosure_hash,
                "hidden_labels": False,
                "original_call_proofs": proof,
                "workflow_completed": procedure_ok,
                "independent_checks": tests,
                "passed": bool(
                    procedure_ok
                    and tests
                    and all(t["exit_code"] == 0 and t["cleanup_confirmed"] and not t["reason"] for t in tests)
                ),
                "claim": "bounded_visible_workflow_validation_not_formal_repair_resolved",
            }
            self.records.mkdir(parents=True, exist_ok=True)
            (self.records / f"{attempt_id}.json").write_text(json.dumps(evidence, indent=2) + "\n")
            return self.store.complete(user, attempt_id, episode, evidence)

    def validate_source(self, loop, user, branch):
        """Repeated repair successes need separate public checks before extraction, too."""
        self.check_disclosure(loop)
        captured = EpisodeIngestor(self.store.episodes).capture(loop, user, branch)
        # The same verifier mechanism runs tests outside the source Agent. Source receipts
        # are stored as host audit events, not a model assertion or final evaluator result.
        owner = loop.session.durable
        with loop.state_lock, loop.session.lock, ExitStack() as stack:
            for path in [owner.lock_path, owner.lock_path.with_suffix(".channel")]:
                lock = ExclusiveFile(path)
                stack.callback(lock.close)
            owner.require_no_containers()
            current = owner.store.read(loop.session.task.task_id)
            if (
                not current["quiescent"]
                or current["lease_epoch"] != owner.lease.lease_epoch
                or current["worker_id"] != owner.lease.worker_id
            ):
                raise ValueError("skill_source_ownership_changed")
            files = loop.session.files.snapshot()
            disclosure_hash = self.check_disclosure(loop)
            if workspace_version(files) != loop.session.task.workspace_version:
                raise ValueError("skill_source_version_changed")
            runner = ContainerRunner(loop.session.runner.image, self.records / "sources" / captured["episode_id"])
            checks = []
            for profile in self.profiles.values():
                call_id = uuid4()
                result = runner.run(
                    loop.session.task.task_id,
                    call_id,
                    0,
                    files,
                    profile,
                    max_seconds=20,
                    max_output_bytes=65536,
                    should_stop=lambda: None,
                )
                process = json.loads((runner.records / f"{call_id}.json").read_text())
                for stream, info in process["output_capture"].items():
                    data = (runner.records / f"{call_id}.{stream}.bin").read_bytes()
                    if sha(data) != info["sha256"] or len(data) != info["size_bytes"]:
                        raise ValueError("skill_source_independent_stream_integrity")
                checks.append(
                    {
                        "call_id": str(call_id),
                        "profile_hash": document_hash(profile),
                        "process_hash": document_hash(process),
                        "passed": result["exit_code"] == 0 and result["cleanup_confirmed"] and not result["reason"],
                    }
                )
            if not checks or not all(c["passed"] for c in checks):
                raise ValueError("skill_source_independent_checks_failed")
            with self.store.semantic.transaction(user) as (c, _):
                self.store._event(
                    c,
                    user,
                    "source_independently_checked",
                    {
                        "episode_id": captured["episode_id"],
                        "document_hash": captured["document_hash"],
                        "suite": self.suite_id,
                        "checks": checks,
                        "hidden_labels": False,
                        "disclosed_suite_hash": disclosure_hash,
                    },
                )
            return captured
