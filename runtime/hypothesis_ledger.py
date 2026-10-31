"""Append-only diagnosis revisions; this is not P2-10's execution/recovery ledger."""

import json
import threading
from datetime import datetime, timezone
from uuid import UUID, uuid4

from domain.failure_diagnosis import Classification, DeclaredExpectation, HypothesisRevision
from domain.models import ArtifactRef
from domain.plans import document_hash
from runtime.diagnostic_evidence import DiagnosticEvidenceStore, environment_hash


class HypothesisLedger:
    def __init__(self, evidence: DiagnosticEvidenceStore):
        self.evidence, self.root = evidence, evidence.root / "ledger"
        self.lock = threading.Lock()

    def save_classification(self, value: Classification):
        facts = [self.evidence.read(identity) for identity in value.evidence_ids]
        if any((f.task_id, f.workspace_id) != (value.task_id, value.workspace_id) for f in facts):
            raise ValueError("classification_evidence_identity_mismatch")
        focus = next(f for f in facts if f.evidence_id == value.focus_evidence_id)
        if focus.workspace_version != value.workspace_version:
            raise ValueError("classification_subject_version_mismatch")
        for identity in value.model_analysis_ids:
            if self.evidence.read_model_analysis(identity).task_id != value.task_id:
                raise ValueError("classification_model_source_mismatch")
        self.validate_expectations(value.declared_expectations)
        path = self.root.parent / "classifications" / f"{value.classification_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(value.model_dump_json(indent=2) + "\n")
        return value

    def read_classification(self, classification_id: UUID):
        from runtime.failure_classifier import classify_failure

        value = Classification.model_validate_json(
            (self.root.parent / "classifications" / f"{classification_id}.json").read_text(encoding="utf-8")
        )
        facts = [self.evidence.read(identity) for identity in value.evidence_ids]
        focus = next(f for f in facts if f.evidence_id == value.focus_evidence_id)
        analyses = tuple(self.evidence.read_model_analysis(identity) for identity in value.model_analysis_ids)
        self.validate_expectations(value.declared_expectations)
        actual = classify_failure(
            focus,
            tuple(f for f in facts if f.evidence_id != focus.evidence_id),
            analyses=analyses,
            expectations=value.declared_expectations,
        )
        if actual.model_dump(exclude={"classification_id"}) != value.model_dump(exclude={"classification_id"}):
            raise ValueError("classification_not_reproducible_from_saved_evidence")
        return value

    def validate_expectations(self, values: tuple[DeclaredExpectation, ...]):
        for value in values:
            fact = self.evidence.read(value.source_evidence_id)
            if fact.operation != "read" or fact.status != "SUCCEEDED":
                raise ValueError("expectation_requires_source_read")
            payload = json.loads(self.evidence.trace.artifacts.read_text(fact.source_artifact))
            if value.quote not in payload.get("result", {}).get("text", ""):
                raise ValueError("expectation_quote_not_in_source")
        return values

    def _history(self, hypothesis_id: UUID):
        values = []
        for path in (self.root / str(hypothesis_id)).glob("*.json"):
            index = json.loads(path.read_text(encoding="utf-8"))
            if set(index) != {"revision_id", "hypothesis_id", "document_hash", "artifact"}:
                raise ValueError("hypothesis_revision_index_mismatch")
            text = self.evidence.trace.artifacts.read_text(ArtifactRef.model_validate(index["artifact"]))
            revision = HypothesisRevision.model_validate_json(text)
            if (
                revision.hypothesis_id != hypothesis_id
                or str(revision.revision_id) != path.stem
                or index["revision_id"] != path.stem
                or index["hypothesis_id"] != str(hypothesis_id)
                or index["document_hash"] != document_hash(revision)
            ):
                raise ValueError("hypothesis_revision_content_mismatch")
            values.append(revision)
        by_id = {v.revision_id: v for v in values}
        roots = [v for v in values if v.parent_revision_id is None]
        if not values:
            return []
        if len(roots) != 1:
            raise ValueError("hypothesis_chain_root_mismatch")
        chain = [roots[0]]
        while True:
            children = [v for v in values if v.parent_revision_id == chain[-1].revision_id]
            if len(children) > 1:
                raise ValueError("hypothesis_chain_fork_requires_inspection")
            if not children:
                break
            child = children[0]
            if child.parent_hash != document_hash(chain[-1]):
                raise ValueError("hypothesis_revision_parent_hash_mismatch")
            chain.append(child)
        if len(chain) != len(by_id):
            raise ValueError("hypothesis_chain_incomplete")
        return chain

    def append(self, *, hypothesis_id: UUID | None = None, expected_parent_id: UUID | None = None, **fields):
        with self.lock:
            identity = hypothesis_id or uuid4()
            history = self._history(identity)
            parent = history[-1] if history else None
            if expected_parent_id != (None if parent is None else parent.revision_id):
                raise ValueError("hypothesis_revision_conflict")
            revision = HypothesisRevision(
                hypothesis_id=identity,
                parent_revision_id=expected_parent_id,
                parent_hash=None if parent is None else document_hash(parent),
                created_at=datetime.now(timezone.utc),
                **fields,
            )
            self._validate(revision)
            if parent:
                if (parent.task_id, parent.workspace_id) != (revision.task_id, revision.workspace_id):
                    raise ValueError("hypothesis_cannot_change_task_workspace")
                if (
                    parent.status == "refuted"
                    and revision.status == "supported"
                    and not any(
                        self.evidence.read(identity).recorded_at > parent.created_at
                        for identity in revision.supporting_evidence_ids
                    )
                ):
                    raise ValueError("refuted_hypothesis_requires_new_observation_to_reopen")
            path = self.root / str(identity) / f"{revision.revision_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                artifact = self.evidence.trace.artifacts.put_text(revision.model_dump_json(indent=2))
                stream.write(
                    json.dumps(
                        {
                            "revision_id": str(revision.revision_id),
                            "hypothesis_id": str(identity),
                            "document_hash": document_hash(revision),
                            "artifact": artifact.model_dump(mode="json"),
                        },
                        indent=2,
                    )
                    + "\n"
                )
            return revision

    def _validate(self, revision: HypothesisRevision):
        referenced = set(revision.supporting_evidence_ids) | set(revision.opposing_evidence_ids)
        referenced |= {e for a in revision.attempts for e in (a.action_evidence_id, a.outcome_evidence_id)}
        facts = {identity: self.evidence.read(identity) for identity in referenced}
        if any((f.task_id, f.workspace_id) != (revision.task_id, revision.workspace_id) for f in facts.values()):
            raise ValueError("hypothesis_evidence_task_mismatch")
        if revision.status == "supported" and not any(
            f.workspace_version == revision.subject_version
            and environment_hash(f.context) == revision.subject_environment_hash
            for f in facts.values()
        ):
            raise ValueError("supported_hypothesis_requires_current_subject_observation")
        for identity in revision.model_analysis_ids:
            if self.evidence.read_model_analysis(identity).task_id != revision.task_id:
                raise ValueError("hypothesis_model_task_mismatch")
        for attempt in revision.attempts:
            if facts[attempt.action_evidence_id].recorded_at > facts[attempt.outcome_evidence_id].recorded_at:
                raise ValueError("attempt_outcome_cannot_precede_action")

    def view(self, *, current_version: str, current_environment_hash: str):
        history, active = [], []
        for path in sorted(self.root.glob("*")):
            chain = self._history(UUID(path.name))
            for revision in chain:
                self._validate(revision)
            latest = chain[-1]
            current = (
                latest.subject_version == current_version
                and latest.subject_environment_hash == current_environment_hash
            )
            state = "current_" + latest.status if current else "historical_stale_" + latest.status
            history.append({"latest": latest.model_dump(mode="json"), "view_state": state, "revisions": len(chain)})
            if current and latest.status in {"proposed", "supported", "inconclusive"}:
                active.append(latest.model_dump(mode="json"))
        return {"hypotheses": history, "active": active, "authority_restored": False, "actions_executed": 0}

    def verify(self):
        count = 0
        for path in self.root.glob("*"):
            for revision in self._history(UUID(path.name)):
                self._validate(revision)
                count += 1
        return {"hypotheses": len(list(self.root.glob("*"))), "revisions": count, "actions_executed": 0}
