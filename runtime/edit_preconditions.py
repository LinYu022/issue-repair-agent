"""Capture immutable before/after file objects before publishing an edit intent."""

import os
import stat

from domain.checkpoints import FileChange, ManifestEntry
from tools.workspace_files import sha


def capture_edit(intent, session, objects):
    args = intent.request["arguments"]
    path = args["path"]
    files = session.files.snapshot()
    old = files.get(path)
    if (None if old is None else sha(old.encode())) != args["expected_sha256"]:
        raise ValueError("file_precondition_conflict")

    def entry(name, text, mode):
        return ManifestEntry(
            path=name, content=objects.put(text.encode()), mode=mode, tracked=name in session.files.baseline
        )

    before = None
    if old is not None:
        root = session.files.root_fd()
        from tools.readonly_workspace import directory_flags

        try:
            parts = path.split("/")
            for part in parts[:-1]:
                child = os.open(part, directory_flags(), dir_fd=root)
                os.close(root)
                root = child
            mode = stat.S_IMODE(os.stat(parts[-1], dir_fd=root, follow_symlinks=False).st_mode)
        finally:
            os.close(root)
        before = entry(path, old, mode)
    destination = args.get("destination")
    if destination is not None:
        if old is None or before is None or destination in files:
            raise ValueError("rename_source_or_target_precondition")
        changes = [
            FileChange(operation="delete", path=path, before=before, after=None),
            FileChange(operation="create", path=destination, before=None, after=entry(destination, old, before.mode)),
        ]
    else:
        after = None if args["text"] is None else entry(path, args["text"], before.mode if before else 0o600)
        changes = [
            FileChange(
                operation="create" if before is None else "delete" if after is None else "modify",
                path=path,
                before=before,
                after=after,
            )
        ]
    return intent.model_copy(
        update={
            "preconditions": {
                **intent.preconditions,
                "file_changes": [c.model_dump(mode="json") for c in changes],
                "rename": None if destination is None else {"from": path, "to": destination},
            }
        }
    )
