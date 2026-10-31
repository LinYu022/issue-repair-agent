"""One-host Linux physical ownership paired with persistent fencing and intent-before-dispatch."""

import json
import os
import signal
import socket
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from domain.execution import CallIntent, ExecutionRefused, WorkerIdentity
from domain.plans import document_hash
from runtime.execution_store import PostgresExecutionStore


def process_identity(pid: int, workspace_key: str):
    if os.name != "posix" or not Path("/proc/sys/kernel/random/boot_id").exists():
        raise ExecutionRefused("durable_worker_requires_linux_proc_and_flock")
    text = Path(f"/proc/{pid}/stat").read_text()
    fields = text[text.rindex(")") + 2 :].split()
    return WorkerIdentity(
        host_id=document_hash(
            {"hostname": socket.gethostname(), "machine": Path("/etc/machine-id").read_text().strip()}
        ),
        boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        pid=pid,
        start_ticks=int(fields[19]),
        workspace_key=workspace_key,
    )


def process_state(identity: WorkerIdentity):
    current = process_identity(os.getpid(), identity.workspace_key)
    if (identity.host_id, identity.boot_id) != (current.host_id, current.boot_id):
        return "unknown_host_or_boot"
    try:
        actual = process_identity(identity.pid, identity.workspace_key)
        if actual != identity:
            return "different_process"
        text = Path(f"/proc/{identity.pid}/stat").read_text()
        return "stopped" if text[text.rindex(")") + 2 :].split()[0] == "Z" else "alive"
    except FileNotFoundError:
        return "stopped"
    except (OSError, ValueError):
        return "unknown"


class ExclusiveFile:
    def __init__(self, path):
        import fcntl

        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fd: int | None = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW"), 0o600)
        try:
            getattr(fcntl, "flock")(self.fd, getattr(fcntl, "LOCK_EX") | getattr(fcntl, "LOCK_NB"))
        except OSError:
            os.close(self.fd)
            raise ExecutionRefused("physical_writer_lock_busy") from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class DurableExecution:
    def __init__(
        self,
        store: PostgresExecutionStore,
        task_id: UUID,
        workspace: Path,
        locks: Path,
        runner,
        *,
        register_task=None,
        authority=None,
        seconds=15,
    ):
        self.store, self.runner, self.task_id = store, runner, task_id
        self.workspace = workspace.resolve(strict=True)
        stat = self.workspace.stat()
        self.workspace_key = document_hash({"path": str(self.workspace), "device": stat.st_dev, "inode": stat.st_ino})
        self.identity = process_identity(os.getpid(), self.workspace_key)
        locks = locks.resolve()
        if locks.is_relative_to(self.workspace):
            raise ExecutionRefused("locks_must_be_outside_task_workspace")
        self.lock_path = locks / (self.workspace_key + ".worker")
        self.lifetime = ExclusiveFile(self.lock_path)
        self.closed = False
        self.seconds = seconds
        self.session: Any = None
        self.stop_event = threading.Event()
        self.heartbeat_error: dict | None = None
        self.runner.owner_lock_fd = self.lifetime.fd
        try:
            if register_task is not None:
                store.register(register_task, self.workspace_key, authority or {}, str(self.lock_path.resolve()))
            row = store.read(task_id)
            if row["lock_path"] != str(self.lock_path.resolve()):
                raise ExecutionRefused("physical_lock_domain_changed")
            if row["worker_identity"] and not row["quiescent"]:
                old = WorkerIdentity.model_validate(row["worker_identity"])
                if process_state(old) not in {"stopped", "different_process"}:
                    raise ExecutionRefused("previous_worker_not_confirmed_stopped")
            self.require_no_containers()
            self.lease = store.claim(task_id, uuid4(), self.identity, seconds=seconds)
        except BaseException:
            if self.runner.owner_lock_fd == self.lifetime.fd:
                self.runner.owner_lock_fd = None
            self.lifetime.close()
            self.closed = True
            raise
        self.thread: threading.Thread | None = None

    def require_no_containers(self):
        found = self.runner.docker(
            "ps",
            "-aq",
            "--filter",
            "label=issue-agent.managed=p2-02",
            "--filter",
            f"label=issue-agent.task={self.task_id}",
        )
        if found.returncode:
            raise ExecutionRefused("process_inventory_unknown_no_takeover")
        if found.stdout.strip():
            raise ExecutionRefused("previous_task_containers_require_cleanup")

    def check(self, *, cancel=True):
        if self.closed or self.identity != process_identity(os.getpid(), self.workspace_key):
            raise ExecutionRefused("worker_process_identity_changed")
        if self.heartbeat_error and cancel:
            raise ExecutionRefused("heartbeat_lost")
        row = self.store.read(self.task_id)
        self.store._live(row, self.lease, cancellation=not cancel)
        return row

    def start_heartbeat(self):
        if self.thread is not None:
            raise ExecutionRefused("heartbeat_already_started")

        def beat():
            while not self.stop_event.wait(self.seconds / 3):
                try:
                    self.store.heartbeat(self.lease, seconds=self.seconds)
                except ExecutionRefused as error:
                    if str(error) != "persistent_task_cancelled":
                        record_failure(error)
                    return
                except BaseException as error:
                    record_failure(error)
                    return

        def record_failure(error):
            self.heartbeat_error = {
                "exception": type(error).__name__,
                "code": str(error) if isinstance(error, ExecutionRefused) else "heartbeat_exception",
                "database_sqlstate": getattr(error.__context__, "sqlstate", None),
            }
            try:
                (self.session.records.root / "heartbeat-failure.json").write_text(
                    json.dumps(self.heartbeat_error, indent=2) + "\n", encoding="utf-8"
                )
            except OSError:
                pass  # Preserve the original heartbeat failure even if diagnostics cannot be written.

        self.thread = threading.Thread(target=beat, daemon=True)
        self.thread.start()

    def attach(self, loop):
        self.check()
        if loop.session.task.task_id != self.task_id:
            raise ExecutionRefused("guard_task_mismatch")
        self.session = loop.session
        self.snapshot_root = self.session.records.root / "checkpoints"
        loop.session.durable = self
        loop.session.runner.identity_observer = lambda value: self.store.process(
            self.lease, UUID(value["call_id"]), value
        )
        self.persist(loop.controller.view())
        self.start_heartbeat()

    def persist(self, view):
        self.check(cancel=False)
        from domain.tasks import Task

        self.store.sync(self.lease, Task.model_validate(view["task"]), {k: v for k, v in view.items() if k != "task"})

    @contextmanager
    def channel(self, intent: CallIntent):
        self.check()
        channel = ExclusiveFile(self.lock_path.with_suffix(".channel"))
        prepared = False
        completed = False
        try:
            self.check()
            if intent.kind == "tool" and intent.operation == "edit" and self.session is not None:
                from runtime.edit_preconditions import capture_edit
                from runtime.snapshot_objects import SnapshotObjects

                with_archive = ExclusiveFile(self.snapshot_root / ".archive.lock")
                try:
                    if not intent.preconditions.get("file_changes"):
                        intent = capture_edit(intent, self.session, SnapshotObjects(self.snapshot_root / "objects"))
                    previous = self.store.prepare(self.lease, intent)
                finally:
                    with_archive.close()
            else:
                previous = self.store.prepare(self.lease, intent)
            if previous is not None:
                yield previous
                return
            prepared = True
            self.store.running(self.lease, intent.call_id)
            yield None
            if self.store.call(self.task_id, intent.call_id)["state"] in {"PREPARED", "RUNNING"}:
                raise ExecutionRefused("dispatch_result_not_committed")
            completed = True
        finally:
            if prepared and not completed:
                try:
                    self.store.finish(
                        self.lease, intent.call_id, "UNKNOWN", {"reason": "dispatch_or_commit_not_confirmed"}
                    )
                except (ValueError, OSError):
                    pass
            channel.close()

    def intent(self, kind, call_id, operation, request, *, preconditions=None):
        task = self.session.task if self.session else None
        if task is None:
            raise ExecutionRefused("session_not_attached")
        conditions = {}
        if kind == "tool" and operation == "edit":
            from tools.workspace_files import workspace_version

            files = self.session.files.snapshot()
            arguments = request["arguments"]
            if arguments["text"] is None:
                old_text = files.pop(arguments["path"], None)
                if arguments.get("destination") is not None:
                    if old_text is None:
                        raise ValueError("rename_source_missing")
                    files[arguments["destination"]] = old_text
            else:
                files[arguments["path"]] = arguments["text"]
            conditions["desired_workspace_version"] = workspace_version(files)
        return CallIntent(
            task_id=self.task_id,
            call_id=call_id,
            kind=kind,
            operation=operation,
            request=request,
            execution_epoch=task.execution_epoch,
            preconditions={
                "workspace_version": task.workspace_version,
                "grant_id": str(task.grant_id),
                "revision_id": str(task.revision_id),
                **conditions,
                **(preconditions or {}),
            },
        )

    def finish(self, intent, status, result):
        self.store.finish(self.lease, intent.call_id, status, result)

    def close(self):
        if self.closed:
            return
        channel = ExclusiveFile(self.lock_path.with_suffix(".channel"))
        try:
            self.stop_event.set()
            if self.thread:
                self.thread.join(timeout=4)
            self.require_no_containers()
            self.store.release(self.lease)
        finally:
            self.closed = True
            if self.runner.owner_lock_fd == self.lifetime.fd:
                self.runner.owner_lock_fd = None
            self.lifetime.close()
            channel.close()

    def reconcile_pending(self, files, records):
        self.check(cancel=False)
        self.require_no_containers()
        from tools.workspace_files import workspace_version

        row = self.store.read(self.task_id)
        call_id = row["active_call_id"]
        if call_id is None:
            raise ExecutionRefused("no_pending_call")
        call = self.store.call(self.task_id, call_id)
        intent = CallIntent.model_validate(call["intent"])
        actual = workspace_version(files.snapshot())
        evidence = {
            "actual_workspace_version": actual,
            "no_task_containers": True,
            "lease_epoch": self.lease.lease_epoch,
        }
        if call["state"] == "PREPARED":
            outcome = "not_applied"
        elif intent.kind == "model":
            outcome = "unknown_model_billing"
        elif intent.operation == "edit":
            outcome = (
                "applied"
                if actual == intent.preconditions.get("desired_workspace_version")
                else "not_applied"
                if actual == intent.preconditions["workspace_version"]
                else "conflict"
            )
        elif actual != intent.preconditions["workspace_version"]:
            outcome = "conflict"
        else:
            outcome = "not_applied"
        path = records.root / "calls" / ("model" if intent.kind == "model" else "tool") / f"{call_id}.json"
        if path.exists():
            rec, request, result = records.read(path)
            if rec.task_id != self.task_id or str(rec.call_id) != str(call_id):
                raise ExecutionRefused("reconciliation_record_mismatch")
            evidence["recorded_result_hash"] = document_hash(result)
            if outcome != "conflict" and rec.status != "UNKNOWN":
                outcome = "recorded_result"
        evidence["outcome"] = outcome
        self.store.reconcile(self.lease, call_id, evidence)
        return evidence

    def snapshot(self):
        """Stable read boundary only. Not a P2-11 COMMITTED checkpoint."""
        if self.session is None:
            raise ExecutionRefused("session_not_attached")
        if not self.session.lock.acquire(blocking=False):
            raise ExecutionRefused("snapshot_while_call_active")
        try:
            intent = self.intent("snapshot", uuid4(), "snapshot_view", {})
            with self.channel(intent):
                from tools.workspace_files import workspace_version

                files = self.session.files.snapshot()
                if workspace_version(files) != self.session.task.workspace_version:
                    raise ExecutionRefused("snapshot_version_conflict")
                ref = self.session.records.artifacts.put_text(json.dumps(files, sort_keys=True, ensure_ascii=False))
                result = {
                    "artifact": ref.model_dump(mode="json"),
                    "workspace_version": workspace_version(files),
                    "checkpoint_committed": False,
                }
                self.finish(intent, "SUCCEEDED", result)
                return result
        finally:
            self.session.lock.release()

    def retry_tool(self, previous_call_id, request):
        """Trusted explicit retry, new call identity and unchanged lifetime budget."""
        if self.session is None or request.call_id == previous_call_id:
            raise ExecutionRefused("retry_requires_new_identity_and_session")
        return self.session.call(request, retry_of=previous_call_id)


def stop_registered_worker(identity: WorkerIdentity):
    """Trusted supervisor, exact proc identity only; never arbitrary PID from an Agent."""
    state = process_state(identity)
    if state in {"stopped", "different_process"}:
        return state
    if state != "alive" or identity.pid == os.getpid():
        raise ExecutionRefused("worker_stop_requires_manual_inspection")
    # PID reuse cannot redirect this signal. Docker clients inherit the owner lock;
    # any surviving host client keeps takeover blocked until it exits.
    descriptor = getattr(os, "pidfd_open")(identity.pid)
    try:
        if process_identity(identity.pid, identity.workspace_key) != identity:
            raise ExecutionRefused("worker_identity_changed_before_signal")
        getattr(signal, "pidfd_send_signal")(descriptor, getattr(signal, "SIGKILL"))
    finally:
        os.close(descriptor)
    return "stop_requested_recheck_required"
