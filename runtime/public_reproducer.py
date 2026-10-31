"""Visible probe composition, immutable binding and bounded process-result checks."""

import base64

from domain.plans import path_subset
from domain.repair_loop import RepairRegistration
from domain.reproduction import PROBE_PATH, PublicReproducerBinding, PublicReproducerSpec
from domain.workspace_tools import TaskPackage, TestProfile, VisibleFile
from tools.workspace_files import sha, workspace_version


def profile(binding: PublicReproducerBinding, profile_id: str) -> TestProfile:
    marker = "ISSUE_PUBLIC_PROBE:" + binding.code_sha256 + ":"
    bootstrap = (
        "import runpy,sys,traceback\n"
        "try:\n runpy.run_path(" + repr(binding.probe_path) + ",run_name='__main__')\n"
        "except AssertionError:\n traceback.print_exc();print("
        + repr(marker + "ASSERTION_FAILED")
        + ",flush=True);sys.exit(1)\n"
        "except BaseException:\n traceback.print_exc();print("
        + repr(marker + "EXECUTION_ERROR")
        + ",flush=True);sys.exit(2)\n"
        "else:\n print(" + repr(marker + "PASSED") + ",flush=True)\n"
    )
    return TestProfile(profile_id=profile_id, argv=("/usr/local/bin/python", "-B", "-c", bootstrap))


def compose(
    package: TaskPackage, registration: RepairRegistration, profiles: dict[str, TestProfile], spec: PublicReproducerSpec
):
    """Trusted operator entry. No code runs on the host and no scope expands."""
    if registration.public_reproducer is not None:
        raise ValueError("public_reproducer_already_bound")
    if set(profiles) != set(registration.scope.test_profiles) or any(
        name != value.profile_id for name, value in profiles.items()
    ):
        raise ValueError("public_reproducer_profile_inventory_mismatch")
    files = {f.path: f.text for f in package.files}
    if (
        (spec.task_id, spec.repo, spec.base_commit)
        != (package.task.task_id, package.task.repo, package.task.base_commit)
        or spec.issue_sha256 != sha(package.task.issue.encode())
        or spec.original_source_version != workspace_version(files)
    ):
        raise ValueError("public_reproducer_task_issue_or_source_mismatch")
    if any(
        p.casefold() == PROBE_PATH.casefold() or p.casefold().startswith(PROBE_PATH.casefold() + "/") for p in files
    ):
        raise ValueError("public_reproducer_reserved_path_collision")
    if path_subset((PROBE_PATH,), registration.scope.write_paths):
        raise ValueError("public_reproducer_path_is_writable")
    binding = PublicReproducerBinding.model_validate(spec.model_dump(exclude={"code"}))
    package = TaskPackage.model_validate(
        {
            **package.model_dump(),
            "files": (*package.files, VisibleFile(path=PROBE_PATH, text=spec.code, sha256=spec.code_sha256)),
        }
    )
    registration = RepairRegistration.model_validate(
        {
            **registration.model_dump(),
            "public_reproducer": binding,
            "protected_paths": (*registration.protected_paths, PROBE_PATH),
        }
    )
    profiles = {**profiles, registration.reproduction_profile: profile(binding, registration.reproduction_profile)}
    verify(package, registration, profiles, {f.path: f.text for f in package.files})
    return package, registration, profiles


def verify(package, registration, profiles, current_files):
    binding = registration.public_reproducer
    if binding is None:
        return
    baseline = {f.path: f.text for f in package.files if f.path != binding.probe_path}
    if (
        (binding.task_id, binding.repo, binding.base_commit)
        != (package.task.task_id, package.task.repo, package.task.base_commit)
        or binding.issue_sha256 != sha(package.task.issue.encode())
        or binding.original_source_version != workspace_version(baseline)
        or binding.probe_path not in registration.protected_paths
        or sha(current_files.get(binding.probe_path, "").encode()) != binding.code_sha256
        or profiles.get(registration.reproduction_profile) != profile(binding, registration.reproduction_profile)
    ):
        raise ValueError("public_reproducer_binding_or_profile_drift")


def outcome(binding, run):
    if binding is None:
        return None
    if not run.get("cleanup_confirmed") or run.get("reason") or run.get("output_truncated"):
        return "unconfirmed"
    try:
        stdout = base64.b64decode(run.get("stdout_base64", ""), validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return "unconfirmed"
    marker = "ISSUE_PUBLIC_PROBE:" + binding.code_sha256 + ":"
    last = stdout.splitlines()[-1] if stdout.splitlines() else ""
    if run.get("exit_code") == 1 and last == marker + "ASSERTION_FAILED":
        return "assertion_failed"
    if run.get("exit_code") == 0 and last == marker + "PASSED":
        return "passed"
    if run.get("exit_code") == 2 and last == marker + "EXECUTION_ERROR":
        return "execution_error"
    return "unconfirmed"
