"""Local content-addressed artifacts for an offline teaching example."""

import hashlib
from pathlib import Path

from domain.models import ArtifactRef


class LocalArtifactStore:
    def __init__(self, root: Path):
        self.root = root

    def put_text(self, text: str) -> ArtifactRef:
        data = text.encode("utf-8")
        ref = ArtifactRef(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
        path = self.root / f"{ref.sha256}.txt"
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as file:
                file.write(data)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("existing artifact content does not match its hash")
        return ref

    def read_text(self, ref: ArtifactRef) -> str:
        data = (self.root / f"{ref.sha256}.txt").read_bytes()
        if len(data) != ref.size_bytes or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ValueError("artifact integrity check failed")
        return data.decode("utf-8")
