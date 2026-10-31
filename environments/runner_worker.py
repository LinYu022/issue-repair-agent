"""Trusted image entrypoint: materialize a sanitized copy, then run one approved test.

This file runs only inside the disposable restricted container. It contains no
host credentials, evaluator data or Docker access. The outer controller removes
the entire container, including detached grandchildren, on every outcome.
"""

import json
import os
import pathlib
import resource
import subprocess
import sys


def main():
    payload = json.loads(sys.stdin.buffer.read(8 * 1024 * 1024 + 1))
    root = pathlib.Path("/workspace")
    for name, text in payload["files"].items():
        path = pathlib.PurePosixPath(name)
        if path.is_absolute() or str(path) != name or ".." in path.parts or "\\" in name:
            raise ValueError("unsafe_worker_path")
        destination = root.joinpath(*path.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("x", encoding="utf-8", newline="") as stream:
            stream.write(text)
    cwd = root / payload["cwd"]
    if not cwd.is_dir() or not cwd.resolve().is_relative_to(root):
        raise ValueError("invalid_worker_cwd")
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    environment = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "PYTHONHASHSEED": "0",
    }
    try:
        result = subprocess.run(
            payload["argv"], cwd=cwd, env=environment, start_new_session=True, timeout=payload["max_seconds"]
        )
    except subprocess.TimeoutExpired:
        return 124  # Exiting PID 1 stops the container namespace, including detached children.
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(main())
