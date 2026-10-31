"""External behavioral oracle: fresh baselines, fresh IDs, no result reuse or Agent feedback."""

import base64
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from domain.workspace_tools import TestProfile
from evals.contracts import AuthorityCase, CaseResult, EvaluationResult, EvaluationSpec, PatchSubmission, content_hash
from evals.patching import apply_patch
from runtime.container_runner import ContainerRunner
from tools.workspace_files import workspace_version

EVALUATOR_SOURCES = (
    "evals/contracts.py",
    "evals/patching.py",
    "evals/oracle.py",
    "environments/oracle_observer.py",
    "runtime/container_runner.py",
    "environments/runner_worker.py",
    "tools/workspace_files.py",
    "domain/workspaces.py",
    "domain/workspace_tools.py",
)


def evaluator_digest(root: Path) -> str:
    return content_hash({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in EVALUATOR_SOURCES})


def classify_cases(
    spec: EvaluationSpec, baseline: tuple[CaseResult, ...], candidate: tuple[CaseResult, ...], *, empty: bool
):
    expected_ids = {case.case_id for case in spec.cases}
    for results in (baseline, candidate):
        if len(results) != len(expected_ids) or {case.case_id for case in results} != expected_ids:
            return "unscorable", "unconfirmed", "authority_case_inventory_incomplete"
        if any(result.group != next(c.group for c in spec.cases if c.case_id == result.case_id) for result in results):
            return "unscorable", "unconfirmed", "authority_case_group_mismatch"
    return classify_observations(baseline, candidate, empty=empty)


def classify_observations(baseline, candidate, *, empty):
    if any(case.state == "environment_error" for case in (*baseline, *candidate)):
        return "environment_error", "unconfirmed", "evaluation_environment_error"
    if any(case.state in {"missing", "skipped", "timeout"} for case in baseline):
        return "unscorable", "unconfirmed", "baseline_authority_not_executed"
    if any(case.state != ("failed" if case.group == "FAIL_TO_PASS" else "passed") for case in baseline):
        return "unscorable", "unconfirmed", "baseline_does_not_establish_f2p_p2p"
    p2p = [case for case in candidate if case.group == "PASS_TO_PASS"]
    regression = (
        "observed"
        if any(case.state == "failed" for case in p2p)
        else "not_observed"
        if all(case.state == "passed" for case in p2p)
        else "unconfirmed"
    )
    if any(case.state in {"missing", "skipped", "timeout"} for case in candidate):
        return "unscorable", regression, "candidate_authority_not_executed"
    if not empty and all(case.state == "passed" for case in candidate):
        return "resolved", regression, "all_authoritative_observations_passed"
    return "unresolved", regression, "empty_patch" if empty else "authoritative_behavior_mismatch"


def decode_observation(case: AuthorityCase, run: dict, nonce: str, call_id: UUID) -> CaseResult:
    fields: dict[str, Any] = {"case_id": case.case_id, "group": case.group, "call_id": call_id}
    if run.get("reason") == "timeout":
        return CaseResult(**fields, state="timeout", reason="case_deadline")
    if not run.get("cleanup_confirmed") or run.get("reason") or run.get("exit_code") in {None, 125, 126, 127, 137}:
        return CaseResult(**fields, state="environment_error", reason="case_runtime_failed")
    if run.get("exit_code") != 0:
        return CaseResult(**fields, state="missing", reason="observer_process_failed")
    try:
        data = json.loads(base64.b64decode(run["stdout_base64"]).decode("utf-8"))
        if not isinstance(data, dict) or data.get("nonce") != nonce:
            raise ValueError("missing_nonce")
        kind = data.get("kind")
        if kind == "skipped" and set(data) == {"nonce", "kind"}:
            return CaseResult(**fields, state="skipped", reason="authority_skip_observed")
        if kind == "exception" and set(data) == {"nonce", "kind", "exception"} and isinstance(data["exception"], str):
            return CaseResult(**fields, state="failed", actual={"exception": data["exception"]})
        if kind != "value" or set(data) != {"nonce", "kind", "value"}:
            raise ValueError("invalid_observation_schema")
        passed = content_hash(data["value"]) == content_hash(case.expected)
        return CaseResult(**fields, state="passed" if passed else "failed", actual=data["value"])
    except (ValueError, UnicodeError, KeyError):
        return CaseResult(**fields, state="missing", reason="invalid_or_missing_observation")


class IndependentEvaluator:
    def __init__(self, root: Path, image_id: str, source_root: Path):
        self.root, self.image_id = root, image_id
        self.digest = evaluator_digest(source_root)

    def observe(self, directory: Path, task_id: UUID, files: dict[str, str], cases: tuple[AuthorityCase, ...]):
        runner = ContainerRunner(self.image_id, directory)
        results = []
        if any(path == "__evaluator_input.json" or path.startswith("__evaluator_/") for path in files):
            raise ValueError("reserved_evaluator_path")
        for case in cases:
            call_id, nonce = uuid4(), uuid4().hex
            directory.mkdir(parents=True, exist_ok=True)
            (directory / f"{call_id}.dispatch.json").write_text(
                json.dumps(
                    {
                        "case_id": case.case_id,
                        "nonce": nonce,
                        "image_id": self.image_id,
                        "probe_hash": hashlib.sha256(case.probe.encode()).hexdigest(),
                    }
                )
                + "\n"
            )
            payload = {**files, "__evaluator_input.json": json.dumps({"nonce": nonce, "probe": case.probe})}
            try:
                run = runner.run(
                    task_id,
                    call_id,
                    1,
                    payload,
                    TestProfile(
                        profile_id="authoritative-observation",
                        argv=("/usr/local/bin/python", "-I", "-S", "/opt/evaluator/oracle_observer.py"),
                    ),
                    max_seconds=case.max_seconds,
                    max_output_bytes=16384,
                    should_stop=lambda: None,
                )
                result = decode_observation(case, run, nonce, call_id)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                result = CaseResult(
                    case_id=case.case_id,
                    group=case.group,
                    call_id=call_id,
                    state="environment_error",
                    reason=type(exc).__name__,
                )
            results.append(result)
        return tuple(results)

    def verify_observations(self, directory: Path, spec: EvaluationSpec, results: tuple[CaseResult, ...]):
        for result in results:
            if result.call_id is None:
                raise ValueError("authoritative_case_execution_id_missing")
            case = next(case for case in spec.cases if case.case_id == result.case_id)
            dispatch = json.loads((directory / f"{result.call_id}.dispatch.json").read_text())
            if (
                dispatch["case_id"] != case.case_id
                or dispatch["image_id"] != self.image_id
                or dispatch["probe_hash"] != hashlib.sha256(case.probe.encode()).hexdigest()
            ):
                raise ValueError("case_dispatch_identity_mismatch")
            process_path = directory / f"{result.call_id}.json"
            if not process_path.exists() and result.state == "environment_error":
                continue
            run = json.loads(process_path.read_text())
            if run["image_id"] != self.image_id:
                raise ValueError("case_process_image_mismatch")
            for name in ("stdout", "stderr"):
                data = (directory / f"{result.call_id}.{name}.bin").read_bytes()
                reference = run["output_capture"][name]
                if len(data) != reference["size_bytes"] or hashlib.sha256(data).hexdigest() != reference["sha256"]:
                    raise ValueError("case_output_integrity_mismatch")
                run[name + "_base64"] = base64.b64encode(data).decode()
            decoded = decode_observation(case, run, dispatch["nonce"], result.call_id)
            if decoded != result:
                raise ValueError("case_observation_verdict_mismatch")

    def evaluate(self, spec: EvaluationSpec, submission: PatchSubmission, *, eval_run_id: UUID | None = None):
        if submission.instance_id != spec.package.task.task_id:
            raise ValueError("submission_instance_mismatch")
        run_id = eval_run_id or uuid4()
        directory = self.root / str(run_id)
        # An old run directory, including an unfinished one, cannot be reused as a new evaluation.
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "started.json").write_text(json.dumps({"eval_run_id": str(run_id), "state": "STARTED"}) + "\n")
        (directory / "submission.json").write_text(submission.model_dump_json(indent=2) + "\n", encoding="utf-8")
        (directory / "rules.json").write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
        baseline_files = {entry.path: entry.text for entry in spec.package.files}
        common: dict[str, Any] = {
            "eval_run_id": run_id,
            "task_id": submission.task_id,
            "instance_id": submission.instance_id,
            "method_id": submission.method_id,
            "repeat_index": submission.repeat_index,
            "origin": spec.origin,
            "base_commit": spec.package.task.base_commit,
            "base_content_hash": workspace_version(baseline_files),
            "patch_hash": hashlib.sha256(submission.patch.encode()).hexdigest(),
            "image_id": self.image_id,
            "evaluator_digest": self.digest,
            "rules_hash": content_hash(spec.model_dump(mode="json")),
            "recorded_at": datetime.now(timezone.utc),
        }
        (directory / "execution-manifest.json").write_text(
            json.dumps(common, default=str, sort_keys=True, indent=2) + "\n"
        )
        try:
            applied = apply_patch(spec.package, submission.patch)
            if any(path == "__evaluator_input.json" or path.startswith("__evaluator_/") for path in applied.files):
                raise ValueError("reserved_evaluator_path")
        except ValueError as exc:
            result = EvaluationResult(
                **common, patch_state="invalid", status="invalid_patch", regression="unconfirmed", reason=str(exc)
            )
        else:
            baseline = self.observe(directory / "baseline", submission.task_id, baseline_files, spec.cases)
            candidate = self.observe(directory / "candidate", submission.task_id, applied.files, spec.cases)
            empty = not applied.changed_paths
            status, regression, reason = classify_cases(spec, baseline, candidate, empty=empty)
            result = EvaluationResult(
                **common,
                effective_patch_hash=applied.effective_hash,
                patch_state="empty" if empty else "nonempty",
                baseline=baseline,
                candidate=candidate,
                status=status,
                regression=regression,
                reason=reason,
            )
        with (directory / "result.json").open("x", encoding="utf-8") as stream:
            stream.write(result.model_dump_json(indent=2) + "\n")
        # No API returns this to an Agent session or updates its Task/self-test state.
        return result

    def read(self, run_id: UUID) -> EvaluationResult:
        directory = self.root / str(run_id)
        result = EvaluationResult.model_validate_json((directory / "result.json").read_text(encoding="utf-8"))
        submission = PatchSubmission.model_validate_json((directory / "submission.json").read_text(encoding="utf-8"))
        spec = EvaluationSpec.model_validate_json((directory / "rules.json").read_text(encoding="utf-8"))
        manifest = json.loads((directory / "execution-manifest.json").read_text())
        for field in (
            "task_id",
            "instance_id",
            "method_id",
            "repeat_index",
            "origin",
            "base_commit",
            "base_content_hash",
            "patch_hash",
            "image_id",
            "evaluator_digest",
            "rules_hash",
        ):
            if str(getattr(result, field)) != str(manifest[field]):
                raise ValueError("evaluation_execution_manifest_mismatch")
        if (
            result.eval_run_id != run_id
            or result.task_id != submission.task_id
            or result.instance_id != submission.instance_id
            or result.method_id != submission.method_id
            or result.repeat_index != submission.repeat_index
            or result.patch_hash != hashlib.sha256(submission.patch.encode()).hexdigest()
            or result.rules_hash != content_hash(spec.model_dump(mode="json"))
        ):
            raise ValueError("evaluation_artifact_identity_or_hash_mismatch")
        if result.status not in {"invalid_patch", "environment_error"}:
            self.verify_observations(directory / "baseline", spec, result.baseline)
            self.verify_observations(directory / "candidate", spec, result.candidate)
            expected = classify_cases(spec, result.baseline, result.candidate, empty=result.patch_state == "empty")
            if expected != (result.status, result.regression, result.reason):
                raise ValueError("evaluation_verdict_inconsistent")
        return result
