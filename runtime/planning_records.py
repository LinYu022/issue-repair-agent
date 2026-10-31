"""Append-only local planning decisions, separate approval and human-hint provenance.

These records aid review. They are not a recovery ledger or authenticated user
identity proof; no server restart automatically restores execution authority.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from domain.plans import Authorization, PlanRevision, document_hash
from runtime.artifacts import LocalArtifactStore


class PlanningRecords:
    @classmethod
    def reopen(cls, root: Path):
        """Explicit audited recovery entry; records confer no authority."""
        value = cls.__new__(cls)
        value.root = root
        value.artifacts = LocalArtifactStore(root / "objects")
        value.events = sorted(
            (json.loads(p.read_text(encoding="utf-8")) for p in (root / "events").glob("*.json")),
            key=lambda e: e["sequence"],
        )
        if [e["sequence"] for e in value.events] != list(range(1, len(value.events) + 1)):
            raise ValueError("planning_recovery_sequence_gap")
        value.verify()
        return value

    def __init__(self, root: Path):
        if root.exists() and any(root.iterdir()):
            raise ValueError("existing_planning_state_requires_explicit_recovery")
        self.root = root
        self.artifacts = LocalArtifactStore(root / "objects")
        self.events: list[dict] = []

    def save(self, kind: str, value: PlanRevision | Authorization):
        identity = value.revision_id if isinstance(value, PlanRevision) else value.authorization_id
        path = self.root / kind / f"{identity}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(value.model_dump_json(indent=2) + "\n")

    def event(self, request_id: UUID, kind: str, actor_role: str, payload: BaseModel | dict):
        path = self.root / "events" / f"{request_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        reference = self.artifacts.put_text(json.dumps(data, sort_keys=True, ensure_ascii=False, indent=2))
        record = {
            "event_id": str(request_id),
            "kind": kind,
            "actor_role": actor_role,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "payload_artifact": reference.model_dump(mode="json"),
            "sequence": len(self.events) + 1,
        }
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(record, indent=2) + "\n")
        self.events.append(record)
        return record

    def snapshot(self, state: dict):
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / "state.pending.json"
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.root / "state.json")

    def verify(self):
        from domain.models import ArtifactRef

        for event in self.events:
            actual = json.loads((self.root / "events" / f"{event['event_id']}.json").read_text())
            if actual != event:
                raise ValueError("planning_event_tampered")
            payload = self.artifacts.read_text(ArtifactRef.model_validate(event["payload_artifact"]))
            data = json.loads(payload)
            if event["kind"] in {"authorization_requested", "plan_revised_within_authorization"}:
                revision = PlanRevision.model_validate_json(
                    (self.root / "revisions" / f"{data['revision_id']}.json").read_text(encoding="utf-8")
                )
                if document_hash(revision) != document_hash(PlanRevision.model_validate(data)):
                    raise ValueError("plan_revision_artifact_mismatch")
            elif event["kind"] == "execution_approved":
                auth_data = data["authorization"]
                auth = Authorization.model_validate_json(
                    (self.root / "authorizations" / f"{auth_data['authorization_id']}.json").read_text(encoding="utf-8")
                )
                if document_hash(auth) != document_hash(Authorization.model_validate(auth_data)):
                    raise ValueError("authorization_artifact_mismatch")
        return {
            "events": len(self.events),
            "approval_events": sum(e["kind"] == "execution_approved" for e in self.events),
            "human_hint_events": sum(e["kind"] == "human_diagnostic_hint" for e in self.events),
        }
