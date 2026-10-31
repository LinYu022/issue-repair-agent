"""Explicit task-local source registry. Arbitrary object hashes and paths are not browsable."""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid5

from domain.context import ArtifactPage, ArtifactSource
from domain.plans import document_hash
from runtime.artifacts import LocalArtifactStore
from tools.workspace_files import sha


class ContextArchive:
    def __init__(self, root: Path, artifacts: LocalArtifactStore, task_id: UUID, workspace_id: UUID):
        self.root, self.artifacts, self.task_id, self.workspace_id = root, artifacts, task_id, workspace_id

    def register(self, text: str, **fields):
        reference = self.artifacts.put_text(text)
        identity = uuid5(
            self.task_id, document_hash({"workspace_id": self.workspace_id, "fields": fields, "artifact": reference})
        )
        existing = self.root / "sources" / f"{identity}.json"
        if existing.exists():
            return self.source(identity)
        source = ArtifactSource(
            source_id=identity,
            task_id=self.task_id,
            workspace_id=self.workspace_id,
            artifact=reference,
            recorded_at=datetime.now(timezone.utc),
            **fields,
        )
        directory = self.root / "sources"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / f"{source.source_id}.json").open("x", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"source": source.model_dump(mode="json"), "source_hash": document_hash(source)},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
        return source

    def source(self, source_id: UUID):
        value = json.loads((self.root / "sources" / f"{source_id}.json").read_text(encoding="utf-8"))
        source = ArtifactSource.model_validate(value["source"])
        if (
            source.source_id != source_id
            or source.task_id != self.task_id
            or source.workspace_id != self.workspace_id
            or document_hash(source) != value["source_hash"]
        ):
            raise ValueError("context_source_identity_or_integrity_mismatch")
        self.artifacts.read_text(source.artifact)
        return source

    def page(self, source_id: UUID, *, current_version: str, offset=0, limit=2048):
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 4096:
            raise ValueError("bounded_context_page_required")
        source = self.source(source_id)
        text = self.artifacts.read_text(source.artifact)
        if offset > len(text):
            raise ValueError("context_page_offset_out_of_range")
        excerpt = text[offset : offset + limit]
        # Preserve complete Unicode codepoints while bounding the encoded response.
        while len(excerpt.encode()) > 8192:
            excerpt = excerpt[:-1]
        end = offset + len(excerpt)
        return ArtifactPage(
            source=source,
            version_state="current_version" if source.workspace_version == current_version else "historical_version",
            offset=offset,
            next_offset=end if end < len(text) else None,
            total_characters=len(text),
            text=excerpt,
            excerpt_sha256=sha(excerpt.encode()),
        )
