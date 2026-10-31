"""Descriptor-only full text tree collection, including modes, directories and untracked files."""

import os
import re
import ssl
import stat

from domain.checkpoints import ManifestEntry, WorkspaceManifest
from domain.workspaces import relative_parts
from tools.readonly_workspace import ReadOnlyWorkspace, WorkspaceRefused, directory_flags, stamp
from tools.workspace_files import FileLimits, workspace_version

CACHES = frozenset({"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv", ".cache", "node_modules"})


def public_certificate(text):
    if set(re.findall(r"-----BEGIN ([^-\r\n]+)-----", text)) != {"CERTIFICATE"}:
        return False
    certificates = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)
    if not certificates:
        return False
    try:
        # PEM bundles may contain Unicode comments. SSL requires ASCII PEM;
        # parse the certificate blocks while retaining the exact original file.
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata="\n".join(certificates))
    except (ssl.SSLError, ValueError, TypeError):
        return False
    return True


def collect_workspace(files, identity, objects, tracked_paths):
    entries, excluded = [], []
    text: dict[str, str] = {}
    seen = total = 0
    tracked_paths = set(tracked_paths)

    def walk(fd, prefix, depth):
        nonlocal seen, total
        if depth > 16:
            raise WorkspaceRefused("manifest_depth_limit")
        before = os.fstat(fd)
        for item in sorted(os.scandir(fd), key=lambda e: e.name):
            seen += 1
            if seen > 1024:
                raise WorkspaceRefused("manifest_entry_limit")
            path = prefix + item.name
            relative_parts(path)
            if item.name == ".gitmodules":
                raise WorkspaceRefused("manifest_submodules_unsupported")
            status = item.stat(follow_symlinks=False)
            if stat.S_ISLNK(status.st_mode) or not (stat.S_ISREG(status.st_mode) or stat.S_ISDIR(status.st_mode)):
                raise WorkspaceRefused("manifest_unsupported_link_or_special_file")
            if status.st_mode & 0o7000:
                raise WorkspaceRefused("manifest_special_permission_bits_unsupported")
            if item.name in CACHES:
                if any(p == path or p.startswith(path + "/") for p in tracked_paths):
                    raise WorkspaceRefused("required_file_conflicts_with_cache_exclusion")
                excluded.append({"path": path, "reason": "declared_disposable_cache"})
                continue
            credential_name = item.name in {"credentials.json", "connection.json", "id_rsa"} or item.name.endswith(
                (".credential.xml", ".pem", ".key")
            )
            if credential_name and not (
                stat.S_ISREG(status.st_mode) and item.name.endswith(".pem") and path in files.baseline
            ):
                raise WorkspaceRefused("manifest_credential_name_requires_curated_package")
            tracked = path in tracked_paths or any(p.startswith(path + "/") for p in tracked_paths)
            if stat.S_ISDIR(status.st_mode):
                child = os.open(item.name, directory_flags(), dir_fd=fd)
                try:
                    entries.append(
                        ManifestEntry(path=path, kind="directory", mode=stat.S_IMODE(status.st_mode), tracked=tracked)
                    )
                    walk(child, path + "/", depth + 1)
                finally:
                    os.close(child)
            else:
                result = ReadOnlyWorkspace.read_file(fd, item.name, FileLimits())
                if credential_name and not (
                    result["text"] == files.baseline[path] and public_certificate(result["text"])
                ):
                    raise WorkspaceRefused("manifest_credential_name_requires_curated_package")
                if "\0" in result["text"]:
                    raise WorkspaceRefused("manifest_binary_nul_unsupported")
                total += result["size_bytes"]
                if total > 4 * 1024 * 1024 or len(text) >= 512:
                    raise WorkspaceRefused("manifest_workspace_size_limit")
                # Verify the directory entry still denotes the same inode and permissions.
                if stamp(status) != stamp(os.stat(item.name, dir_fd=fd, follow_symlinks=False)):
                    raise WorkspaceRefused("manifest_file_changed_during_capture")
                ref = objects.put(result["text"].encode())
                entries.append(
                    ManifestEntry(path=path, content=ref, mode=stat.S_IMODE(status.st_mode), tracked=tracked)
                )
                text[path] = result["text"]
        if stamp(before) != stamp(os.fstat(fd)):
            raise WorkspaceRefused("manifest_directory_changed_during_capture")

    root = files.root_fd()
    try:
        root_mode = stat.S_IMODE(os.fstat(root).st_mode)
        if root_mode & 0o7000:
            raise WorkspaceRefused("manifest_root_special_permissions_unsupported")
        walk(root, "", 0)
    finally:
        os.close(root)
    return WorkspaceManifest(
        task_id=identity.task_id,
        workspace_id=identity.workspace_id,
        repo=identity.repo,
        base_commit=identity.base_commit,
        root_mode=root_mode,
        entries=tuple(entries),
        content_version=workspace_version(text),
        excluded=tuple(excluded),
    )
