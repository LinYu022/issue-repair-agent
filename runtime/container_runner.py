"""One disposable, unprivileged, offline container per test call; no host mounts."""

import base64
import hashlib
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from domain.workspace_tools import TestProfile
from tools.readonly_workspace import WorkspaceRefused, require_linux_capabilities


@dataclass(frozen=True)
class ContainerLimits:
    memory_bytes: int = 268435456
    cpus: str = "1"
    pids: int = 64
    workspace_bytes: int = 33554432

    def __post_init__(self):
        if (
            type(self.memory_bytes) is not int
            or not 67108864 <= self.memory_bytes <= 536870912
            or self.cpus not in {"0.5", "1", "2"}
            or type(self.pids) is not int
            or not 16 <= self.pids <= 128
            or type(self.workspace_bytes) is not int
            or not 8388608 <= self.workspace_bytes <= 67108864
        ):
            raise ValueError("bounded_container_limits_required")


class ContainerRunner:
    def __init__(self, image: str, records: Path, limits: ContainerLimits = ContainerLimits()):
        # Local built image IDs are immutable; no tags, pull, mounts or arbitrary engine options.
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("immutable_local_runner_image_id_required")
        self.image, self.records, self.limits = image, records, limits
        self.active: dict[UUID, str] = {}
        self.lock = threading.Lock()
        self.identity_observer: Callable[[dict], None] | None = None
        self.owner_lock_fd: int | None = None

    def child_options(self):
        return {"pass_fds": (self.owner_lock_fd,)} if self.owner_lock_fd is not None else {}

    def docker(self, *args, timeout=15):
        return subprocess.run(
            ["docker", *args], capture_output=True, timeout=timeout, check=False, **self.child_options()
        )

    def remove(self, name):
        removed = self.docker("rm", "--force", name)
        inspect = self.docker("container", "inspect", name)
        if inspect.returncode != 0 and (removed.returncode == 0 or b"No such container" in inspect.stderr):
            return True
        return False

    def run(
        self,
        task_id: UUID,
        call_id: UUID,
        epoch: int,
        files: dict[str, str],
        profile: TestProfile,
        *,
        max_seconds: float,
        max_output_bytes: int,
        should_stop: Callable[[], str | None],
    ) -> dict:
        require_linux_capabilities()
        if not 0 < max_seconds <= 120 or not 0 < max_output_bytes <= 1048576:
            raise ValueError("bounded_run_limits_required")
        name = "ira-" + call_id.hex
        limits = self.limits
        command = [
            "docker",
            "create",
            "--interactive",
            "--pull",
            "never",
            "--name",
            name,
            "--label",
            "issue-agent.managed=p2-02",
            "--label",
            f"issue-agent.task={task_id}",
            "--label",
            f"issue-agent.epoch={epoch}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--user",
            "65532:65532",
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--cpus",
            limits.cpus,
            "--pids-limit",
            str(limits.pids),
            "--ulimit",
            "nofile=128:128",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            f"/workspace:rw,nosuid,nodev,noexec,size={limits.workspace_bytes},uid=65532,gid=65532,mode=0700",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=16777216,uid=65532,gid=65532,mode=0700",
            "--log-driver",
            "none",
            "--hostname",
            "issue-task",
            # Docker client proxy defaults must not expose operator proxy credentials.
            "--env",
            "HTTP_PROXY=",
            "--env",
            "HTTPS_PROXY=",
            "--env",
            "ALL_PROXY=",
            "--env",
            "NO_PROXY=",
            "--env",
            "http_proxy=",
            "--env",
            "https_proxy=",
            "--env",
            "all_proxy=",
            "--env",
            "no_proxy=",
            self.image,
        ]
        self.records.mkdir(parents=True, exist_ok=True)
        record_path = self.records / f"{call_id}.json"
        identity = {
            "task_id": str(task_id),
            "call_id": str(call_id),
            "execution_epoch": epoch,
            "container_name": name,
            "image_id": self.image,
            "profile_id": profile.profile_id,
            "state": "STARTING",
            "cleanup_confirmed": False,
        }
        record_path.write_text(json.dumps(identity, indent=2) + "\n")
        if self.identity_observer:
            self.identity_observer(identity)
        with self.lock:
            if task_id in self.active:
                raise WorkspaceRefused("task_test_already_active")
            self.active[task_id] = name
        streams: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
        output_lock = threading.Lock()
        overflow = threading.Event()

        def drain(pipe, key):
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                with output_lock:
                    remaining = max_output_bytes - sum(len(value) for value in streams.values())
                    streams[key].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        overflow.set()

        process = None
        threads = []
        writer = None
        start = time.monotonic()
        reason = None
        exit_code = None
        cleanup = False
        try:
            reason = should_stop()
            if not reason:
                created = subprocess.run(command, capture_output=True, timeout=20, **self.child_options())
                if created.returncode:
                    raise WorkspaceRefused("container_create_failed")
                identity["container_id"] = created.stdout.decode("ascii").strip()
                if self.identity_observer:
                    self.identity_observer(identity)
                process = subprocess.Popen(
                    ["docker", "start", "--attach", "--interactive", name],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    **self.child_options(),
                )
                for key in streams:
                    thread = threading.Thread(target=drain, args=(getattr(process, key), key), daemon=True)
                    thread.start()
                    threads.append(thread)
                # Bounded input is fed on a separate thread so cancellation also covers startup.
                payload = json.dumps(
                    {"files": files, "argv": profile.argv, "cwd": profile.cwd, "max_seconds": max_seconds + 2},
                    ensure_ascii=False,
                ).encode()

                def feed():
                    try:
                        assert process.stdin is not None
                        process.stdin.write(payload)
                        process.stdin.close()
                    except (BrokenPipeError, OSError, ValueError):
                        pass

                writer = threading.Thread(target=feed, daemon=True)
                writer.start()
                identity.update(state="RUNNING", docker_client_pid=process.pid)
                record_path.write_text(json.dumps(identity, indent=2) + "\n")
                if self.identity_observer:
                    self.identity_observer(identity)
                while process.poll() is None:
                    reason = should_stop()
                    if not reason and overflow.is_set():
                        reason = "output_limit"
                    if not reason and time.monotonic() - start >= max_seconds:
                        reason = "timeout"
                    if reason:
                        break
                    time.sleep(0.03)
                if not reason:
                    exit_code = process.wait(timeout=2)
                    if overflow.is_set():
                        reason = "output_limit"
                inspected = self.docker("container", "inspect", name)
                if inspected.returncode == 0:
                    info = json.loads(inspected.stdout)[0]
                    identity.update(container_id=info["Id"], engine_state=info["State"])
        finally:
            # Container removal is the process-tree boundary, even for setsid/double-fork children.
            try:
                cleanup = self.remove(name)
            except (OSError, subprocess.SubprocessError):
                cleanup = False
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    cleanup = False
                for thread in threads:
                    thread.join(timeout=3)
                if writer is not None:
                    writer.join(timeout=3)
                if any(thread.is_alive() for thread in threads):
                    cleanup = False
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            with self.lock:
                self.active.pop(task_id, None)
            # Fast-exiting processes can overflow while the last pipe bytes drain.
            if overflow.is_set() and reason is None:
                reason = "output_limit"
            captured = {}
            for key, data in streams.items():
                path = self.records / f"{call_id}.{key}.bin"
                path.write_bytes(data)
                captured[key] = {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
            identity.update(
                state="FINISHED" if cleanup else "CLEANUP_UNCONFIRMED",
                reason=reason,
                exit_code=exit_code,
                cleanup_confirmed=cleanup,
                wall_seconds=round(time.monotonic() - start, 6),
                output_capture=captured,
                output_truncated=overflow.is_set(),
            )
            record_path.write_text(json.dumps(identity, indent=2) + "\n")
            if self.identity_observer:
                self.identity_observer(identity)
        if not cleanup:
            raise WorkspaceRefused("container_cleanup_unconfirmed")
        return {
            **identity,
            "stdout_base64": base64.b64encode(streams["stdout"]).decode(),
            "stderr_base64": base64.b64encode(streams["stderr"]).decode(),
            "captured_bytes": sum(len(data) for data in streams.values()),
            "output_truncated": overflow.is_set(),
            "workspace_changes_discarded": True,
        }

    def reap(self, task_id: UUID) -> list[str]:
        """Explicit operator cleanup after controller loss; never automatically rerun a test."""
        found = self.docker(
            "ps", "-aq", "--filter", "label=issue-agent.managed=p2-02", "--filter", f"label=issue-agent.task={task_id}"
        )
        if found.returncode:
            raise WorkspaceRefused("container_inventory_unavailable")
        ids = found.stdout.decode("ascii").split()
        for identifier in ids:
            if not re.fullmatch(r"[0-9a-f]{12,64}", identifier) or not self.remove(identifier):
                raise WorkspaceRefused("container_cleanup_unconfirmed")
        return ids
