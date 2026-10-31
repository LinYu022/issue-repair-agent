"""Version lifecycle and evidence-backed visible validation, serialized by tenant epoch."""

from datetime import datetime, timezone
from uuid import uuid4

from psycopg.types.json import Jsonb

from domain.execution import ExecutionRefused
from domain.plans import document_hash
from domain.procedural_memory import SkillDocument, SkillRef
from runtime.episode_store import EpisodeStore, migrate_episodic_memory
from runtime.execution_store import SQL_ROOT
from runtime.semantic_rules import applies, terms
from runtime.skill_rules import bind, extract
from tools.workspace_files import sha


def migrate_procedural_memory(connection, direction="up"):
    if direction not in {"up", "down", "status"}:
        raise ExecutionRefused("skill_migration_direction")
    checksum = document_hash({k: (SQL_ROOT / f"0007_procedural_memory.{k}.sql").read_text() for k in ("up", "down")})
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(2026102815)")
        if migrate_episodic_memory(connection, "status") != 1:
            raise ExecutionRefused("skill_requires_episodic_schema")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS public.agent_skill_schema(version integer PRIMARY KEY,checksum text NOT NULL)"
        )
        rows = connection.execute("SELECT version,checksum FROM public.agent_skill_schema").fetchall()
        if rows and rows != [(1, checksum)]:
            raise ExecutionRefused("skill_migration_checksum")
        if direction == "up" and not rows:
            connection.execute((SQL_ROOT / "0007_procedural_memory.up.sql").read_text())
            connection.execute("INSERT INTO public.agent_skill_schema VALUES(1,%s)", (checksum,))
            return 1
        if direction == "down" and rows:
            connection.execute("LOCK TABLE public.agent_skills IN ACCESS EXCLUSIVE MODE")
            if connection.execute("SELECT EXISTS(SELECT 1 FROM public.agent_skills)").fetchone()[0]:
                raise ExecutionRefused("skill_downgrade_data_exists")
            connection.execute((SQL_ROOT / "0007_procedural_memory.down.sql").read_text())
            connection.execute("DELETE FROM public.agent_skill_schema")
            return 0
        return int(bool(rows))


class SkillStore:
    def __init__(self, execution):
        self.execution = execution
        self.episodes = EpisodeStore(execution)
        self.semantic = self.episodes.semantic

    @staticmethod
    def _event(c, user, kind, details, *, request_id=None, input_hash=None):
        payload = {"kind": kind, "details": details, "input_hash": input_hash}
        c.execute(
            "INSERT INTO public.agent_skill_events(user_id,sequence,request_id,payload,payload_hash) SELECT %s,COALESCE(MAX(sequence),0)+1,%s,%s,%s FROM public.agent_skill_events WHERE user_id=%s",
            (user, request_id, Jsonb(payload), document_hash(payload), user),
        )

    @staticmethod
    def _entry(c, user, ref):
        row = c.execute(
            "SELECT r.*,s.user_id,s.enabled_revision FROM public.agent_skill_revisions r JOIN public.agent_skills s USING(skill_id) WHERE s.user_id=%s AND r.skill_id=%s AND r.revision=%s",
            (user, ref.skill_id, ref.revision),
        ).fetchone()
        if row is None:
            raise ExecutionRefused("skill_not_found")
        doc = SkillDocument.model_validate(row["document"])
        if (
            document_hash(doc) != row["document_hash"]
            or row["document_hash"] != ref.document_hash
            or (doc.skill_id, doc.revision, doc.user_id) != (ref.skill_id, ref.revision, user)
        ):
            raise ExecutionRefused("skill_revision_integrity_or_hash_changed")
        return row, doc

    def _sources_live(self, c, doc):
        refs = c.execute(
            "SELECT episode_id,document_hash FROM public.agent_skill_sources WHERE skill_id=%s AND revision=%s",
            (doc.skill_id, doc.revision),
        ).fetchall()
        expected = {(r.episode_id, r.document_hash) for r in doc.sources}
        # Successful validation tasks become revocation dependencies too.
        if not expected <= {(r["episode_id"], r["document_hash"]) for r in refs}:
            raise ExecutionRefused("skill_source_inventory_integrity")
        for ref in refs:
            row, _ = self.episodes._entry(c, doc.user_id, ref["episode_id"])
            if row["state"] != "ACTIVE" or row["document_hash"] != ref["document_hash"]:
                return False
        return True

    def extract(self, user, name, refs, *, parent=None, fault=None):
        with self.semantic.transaction(user) as (c, _):
            documents, blobs = [], {}
            for ref in refs:
                row, doc = self.episodes._entry(c, user, ref.episode_id)
                if row["state"] != "ACTIVE" or row["document_hash"] != ref.document_hash:
                    raise ExecutionRefused("skill_source_inactive")
                receipts = c.execute(
                    "SELECT payload,payload_hash FROM public.agent_skill_events WHERE user_id=%s AND payload->>'kind'='source_independently_checked' AND payload->'details'->>'episode_id'=%s",
                    (user, str(ref.episode_id)),
                ).fetchall()
                if not any(
                    document_hash(r["payload"]) == r["payload_hash"]
                    and r["payload"]["details"]["document_hash"] == ref.document_hash
                    and r["payload"]["details"]["checks"]
                    and all(v["passed"] for v in r["payload"]["details"]["checks"])
                    for r in receipts
                ):
                    raise ExecutionRefused("skill_source_independent_visible_verification_required")
                documents.append(doc)
                values = c.execute(
                    "SELECT * FROM public.agent_episode_blobs WHERE episode_id=%s", (doc.episode_id,)
                ).fetchall()
                values = {str(v["anchor_id"]): v for v in values}
                for event in doc.events:
                    value = values.get(str(event.anchor_id))
                    if (
                        value is None
                        or sha(value["text"].encode()) != event.text.sha256
                        or len(value["text"].encode()) != event.text.size_bytes
                        or event.text_truncated
                    ):
                        raise ExecutionRefused("skill_source_body_incomplete_or_corrupt")
                blobs[str(doc.episode_id)] = {key: value["text"] for key, value in values.items()}
            family = c.execute(
                "SELECT * FROM public.agent_skills WHERE user_id=%s AND name=%s", (user, name)
            ).fetchone()
            if family:
                if (
                    parent is None
                    or parent.skill_id != family["skill_id"]
                    or parent.revision != family["latest_revision"]
                ):
                    raise ExecutionRefused("skill_latest_parent_required")
                previous, _ = self._entry(c, user, parent)
                if previous["state"] == "REVOKED":
                    raise ExecutionRefused("skill_revoked_parent")
                skill_id, revision = parent.skill_id, parent.revision + 1
            else:
                if parent:
                    raise ExecutionRefused("skill_parent_not_found")
                skill_id, revision = uuid4(), 1
            doc = extract(
                documents,
                blobs,
                skill_id=skill_id,
                revision=revision,
                name=name,
                parent_hash=parent.document_hash if parent else None,
            )
            if not family:
                c.execute("INSERT INTO public.agent_skills VALUES(%s,%s,%s,%s,NULL)", (skill_id, user, name, revision))
            else:
                c.execute("UPDATE public.agent_skills SET latest_revision=%s WHERE skill_id=%s", (revision, skill_id))
            c.execute(
                "INSERT INTO public.agent_skill_revisions(skill_id,revision,document,document_hash,state) VALUES(%s,%s,%s,%s,'CANDIDATE')",
                (skill_id, revision, Jsonb(doc.model_dump(mode="json")), document_hash(doc)),
            )
            for ref in refs:
                c.execute(
                    "INSERT INTO public.agent_skill_sources VALUES(%s,%s,%s,%s)",
                    (skill_id, revision, ref.episode_id, ref.document_hash),
                )
            self._event(
                c,
                user,
                "candidate_extracted",
                {"skill_id": str(skill_id), "revision": revision, "document_hash": document_hash(doc)},
            )
            if fault:
                fault("skill_before_candidate_commit")
            self.semantic._bump(c, user)
            return SkillRef(skill_id=skill_id, revision=revision, document_hash=document_hash(doc))

    def inspect(self, user, ref):
        with self.semantic.transaction(user) as (c, _):
            row, doc = self._entry(c, user, ref)
            return {
                "ref": ref.model_dump(mode="json"),
                "state": row["state"],
                "document": doc.model_dump(mode="json"),
                "available_at": row["available_at"].isoformat(),
            }

    def register_validation(self, user, declaration):
        with self.semantic.transaction(user) as (c, _):
            task = c.execute(
                "SELECT payload FROM public.agent_execution_tasks WHERE task_id=%s", (declaration.task_id,)
            ).fetchone()
            if task is None or task["payload"]["source_task_id"] != declaration.instance_id:
                raise ExecutionRefused("skill_validation_registered_task_identity")
            old = c.execute(
                "SELECT declaration_hash FROM public.agent_skill_validation_tasks WHERE user_id=%s AND task_id=%s",
                (user, declaration.task_id),
            ).fetchone()
            if old and old["declaration_hash"] != document_hash(declaration):
                raise ExecutionRefused("skill_validation_registration_conflict")
            c.execute(
                "INSERT INTO public.agent_skill_validation_tasks(user_id,task_id,declaration,declaration_hash) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (user, declaration.task_id, Jsonb(declaration.model_dump(mode="json")), document_hash(declaration)),
            )

    def propose(self, user, ref, query, parameters, profiles, *, mode="use"):
        rejected = None
        with self.semantic.transaction(user) as (c, _):
            row, doc = self._entry(c, user, ref)
            try:
                if query.as_of > datetime.now(timezone.utc) or row["available_at"] > query.as_of:
                    raise ExecutionRefused("skill_not_yet_available")
                if not self._sources_live(c, doc):
                    raise ExecutionRefused("skill_sources_revoked")
                if mode == "use":
                    if (
                        row["state"] != "ENABLED"
                        or row["enabled_revision"] != ref.revision
                        or row["enabled_at"] > query.as_of
                    ):
                        raise ExecutionRefused("skill_current_enabled_version_required")
                elif mode == "validation":
                    declaration = c.execute(
                        "SELECT * FROM public.agent_skill_validation_tasks WHERE user_id=%s AND task_id=%s",
                        (user, query.task_id),
                    ).fetchone()
                    if (
                        declaration is None
                        or document_hash(declaration["declaration"]) != declaration["declaration_hash"]
                        or row["state"] not in {"CANDIDATE", "VALIDATED"}
                    ):
                        raise ExecutionRefused("skill_visible_validation_registration_required")
                    if any(
                        self.episodes._entry(c, user, source.episode_id)[1].source_task_id == query.task_id
                        or self.episodes._entry(c, user, source.episode_id)[1].source_instance_id
                        == declaration["declaration"]["instance_id"]
                        for source in doc.sources
                    ):
                        raise ExecutionRefused("skill_validation_must_transfer_outside_sources")
                else:
                    raise ExecutionRefused("skill_attempt_mode")
                proposal = bind(doc, query, parameters, profiles)
                proposal["skill_ref"] = ref.model_dump(mode="json")
                old = c.execute(
                    "SELECT attempt_id,proposal_hash,state,mode FROM public.agent_skill_attempts WHERE user_id=%s AND skill_id=%s AND revision=%s AND task_id=%s",
                    (user, ref.skill_id, ref.revision, query.task_id),
                ).fetchone()
                if old:
                    if old["proposal_hash"] != document_hash(proposal) or old["mode"] != mode:
                        raise ExecutionRefused("skill_attempt_identity_conflict")
                    return {**proposal, "attempt_id": str(old["attempt_id"])}
                attempt_id = uuid4()
                c.execute(
                    "INSERT INTO public.agent_skill_attempts(attempt_id,user_id,skill_id,revision,task_id,mode,proposal,proposal_hash,state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'PROPOSED')",
                    (
                        attempt_id,
                        user,
                        ref.skill_id,
                        ref.revision,
                        query.task_id,
                        mode,
                        Jsonb(proposal),
                        document_hash(proposal),
                    ),
                )
                self._event(
                    c,
                    user,
                    "draft_proposed",
                    {
                        "attempt_id": str(attempt_id),
                        "task_id": str(query.task_id),
                        "ref": ref.model_dump(mode="json"),
                        "mode": mode,
                    },
                )
                return {**proposal, "attempt_id": str(attempt_id)}
            except ValueError as exc:
                rejected = str(exc)
                self._event(
                    c,
                    user,
                    "proposal_rejected",
                    {"task_id": str(query.task_id), "ref": ref.model_dump(mode="json"), "reason": rejected},
                )
        raise ExecutionRefused(rejected or "skill_proposal_rejected")

    def complete(self, user, attempt_id, episode, evidence):
        """Trusted verifier only: evidence comes from current closed-loop original records."""
        with self.semantic.transaction(user) as (c, _):
            attempt = c.execute(
                "SELECT * FROM public.agent_skill_attempts WHERE user_id=%s AND attempt_id=%s", (user, attempt_id)
            ).fetchone()
            if not attempt or document_hash(attempt["proposal"]) != attempt["proposal_hash"]:
                raise ExecutionRefused("skill_attempt_integrity")
            if attempt["task_id"] != episode.source_task_id or evidence["task_id"] != str(attempt["task_id"]):
                raise ExecutionRefused("skill_attempt_wrong_task")
            row, doc = self._entry(c, user, SkillRef.model_validate(attempt["proposal"]["skill_ref"]))
            source, _ = self.episodes._entry(c, user, episode.episode_id)
            if (
                source["document_hash"] != document_hash(episode)
                or source["state"] != "ACTIVE"
                or not self._sources_live(c, doc)
            ):
                raise ExecutionRefused("skill_attempt_sources_inactive")
            passed = evidence["passed"] and (
                row["state"] in {"CANDIDATE", "VALIDATED"}
                if attempt["mode"] == "validation"
                else row["state"] == "ENABLED" and row["enabled_revision"] == doc.revision
            )
            if attempt["state"] != "PROPOSED":
                if attempt["evidence_hash"] != document_hash(evidence):
                    raise ExecutionRefused("skill_completed_attempt_changed")
                return {"passed": attempt["state"] == "PASSED", "reused": True}
            c.execute(
                "UPDATE public.agent_skill_attempts SET evidence=%s,evidence_hash=%s,state=%s,completed_at=clock_timestamp() WHERE attempt_id=%s",
                (Jsonb(evidence), document_hash(evidence), "PASSED" if passed else "FAILED", attempt_id),
            )
            self._event(
                c,
                user,
                "attempt_passed" if passed else "attempt_failed",
                {
                    "attempt_id": str(attempt_id),
                    "task_id": str(attempt["task_id"]),
                    "evidence_hash": document_hash(evidence),
                },
            )
            if passed and attempt["mode"] == "validation":
                c.execute(
                    "INSERT INTO public.agent_skill_sources VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (doc.skill_id, doc.revision, episode.episode_id, document_hash(episode)),
                )
                c.execute(
                    "UPDATE public.agent_skill_revisions SET state='VALIDATED',validated_at=clock_timestamp() WHERE skill_id=%s AND revision=%s AND state='CANDIDATE'",
                    (doc.skill_id, doc.revision),
                )
                self.semantic._bump(c, user)
            return {
                "passed": bool(passed),
                "reused": False,
                "independent_visible_validation": attempt["mode"] == "validation",
                "formal_resolved": False,
            }

    def decide(self, user, decision):
        with self.semantic.transaction(user) as (c, _):
            old = c.execute(
                "SELECT payload,payload_hash FROM public.agent_skill_events WHERE user_id=%s AND request_id=%s",
                (user, decision.request_id),
            ).fetchone()
            if old:
                if document_hash(old["payload"]) != old["payload_hash"] or old["payload"][
                    "input_hash"
                ] != document_hash(decision):
                    raise ExecutionRefused("skill_decision_id_conflict")
                return old["payload"]["details"]
            row, doc = self._entry(c, user, decision.target)
            enabled = row["enabled_revision"]
            if enabled is None:
                if decision.expected_enabled is not None:
                    raise ExecutionRefused("skill_enabled_pointer_changed")
            else:
                expected = decision.expected_enabled
                if expected is None or expected.skill_id != doc.skill_id or expected.revision != enabled:
                    raise ExecutionRefused("skill_enabled_pointer_changed")
                self._entry(c, user, expected)
            if decision.action in {"enable", "rollback"}:
                wanted = "VALIDATED" if decision.action == "enable" else "DEPRECATED"
                if row["state"] != wanted or not self._sources_live(c, doc) or row["validated_at"] is None:
                    raise ExecutionRefused("skill_separate_validation_required_or_version_revoked")
                proofs = c.execute(
                    "SELECT proposal,proposal_hash,evidence,evidence_hash FROM public.agent_skill_attempts WHERE user_id=%s AND skill_id=%s AND revision=%s AND mode='validation' AND state='PASSED'",
                    (user, doc.skill_id, doc.revision),
                ).fetchall()
                if not proofs:
                    raise ExecutionRefused("skill_validation_proof_missing")
                if any(
                    document_hash(proof["proposal"]) != proof["proposal_hash"]
                    or document_hash(proof["evidence"]) != proof["evidence_hash"]
                    or proof["proposal"]["skill_ref"] != decision.target.model_dump(mode="json")
                    for proof in proofs
                ):
                    raise ExecutionRefused("skill_validation_proof_integrity")
                if enabled:
                    c.execute(
                        "UPDATE public.agent_skill_revisions SET state='DEPRECATED' WHERE skill_id=%s AND revision=%s",
                        (doc.skill_id, enabled),
                    )
                c.execute(
                    "UPDATE public.agent_skill_revisions SET state='ENABLED',enabled_at=clock_timestamp() WHERE skill_id=%s AND revision=%s",
                    (doc.skill_id, doc.revision),
                )
                c.execute(
                    "UPDATE public.agent_skills SET enabled_revision=%s WHERE skill_id=%s", (doc.revision, doc.skill_id)
                )
                state = "ENABLED"
            else:
                if row["state"] == "REVOKED":
                    raise ExecutionRefused("skill_revocation_is_terminal")
                state = "REVOKED" if decision.action == "revoke" else "DEPRECATED"
                c.execute(
                    "UPDATE public.agent_skill_revisions SET state=%s WHERE skill_id=%s AND revision=%s",
                    (state, doc.skill_id, doc.revision),
                )
                if enabled == doc.revision:
                    c.execute("UPDATE public.agent_skills SET enabled_revision=NULL WHERE skill_id=%s", (doc.skill_id,))
            details = {
                "target": decision.target.model_dump(mode="json"),
                "state": state,
                "action": decision.action,
                "reason": decision.reason,
            }
            self._event(
                c,
                user,
                "lifecycle_decision",
                details,
                request_id=decision.request_id,
                input_hash=document_hash(decision),
            )
            self.semantic._bump(c, user)
            return details

    def search(self, user, query, *, record=True):
        if query.as_of > datetime.now(timezone.utc):
            raise ExecutionRefused("skill_future_query_refused")
        with self.semantic.transaction(user) as (c, _):
            rows = c.execute(
                "SELECT r.skill_id,r.revision,r.document_hash FROM public.agent_skill_revisions r JOIN public.agent_skills s USING(skill_id) WHERE s.user_id=%s AND s.enabled_revision=r.revision AND r.state='ENABLED' AND r.enabled_at<=%s AND r.available_at<=%s",
                (user, query.as_of, query.as_of),
            ).fetchall()
            result = []
            for item in rows:
                ref = SkillRef.model_validate(item)
                row, doc = self._entry(c, user, ref)
                if not applies(doc, query) or not self._sources_live(c, doc):
                    continue
                score = len(terms(query.text) & terms(doc.purpose + " " + doc.name))
                result.append(
                    (
                        score,
                        str(doc.skill_id),
                        {
                            "ref": ref.model_dump(mode="json"),
                            "document": doc.model_dump(mode="json"),
                            "available_at": row["available_at"].isoformat(),
                            "grants_authority": False,
                            "execution": "plan_draft_only_current_approval_required",
                        },
                    )
                )
            result.sort(key=lambda value: (-value[0], value[1]))
            items = [v[2] for v in result[: query.limit]]
            if record:
                self._event(
                    c,
                    user,
                    "recalled",
                    {
                        "task_id": str(query.task_id),
                        "refs": [v["ref"] for v in items],
                        "query_hash": document_hash(query),
                    },
                )
            return items

    def audit(self, user):
        with self.semantic.transaction(user) as (c, _):
            events = c.execute(
                "SELECT sequence,payload,payload_hash FROM public.agent_skill_events WHERE user_id=%s ORDER BY sequence",
                (user,),
            ).fetchall()
            for i, event in enumerate(events, 1):
                if event["sequence"] != i or document_hash(event["payload"]) != event["payload_hash"]:
                    raise ExecutionRefused("skill_event_integrity")
            attempts = c.execute(
                "SELECT * FROM public.agent_skill_attempts WHERE user_id=%s ORDER BY created_at", (user,)
            ).fetchall()
            for attempt in attempts:
                if document_hash(attempt["proposal"]) != attempt["proposal_hash"] or (
                    attempt["evidence"] is not None and document_hash(attempt["evidence"]) != attempt["evidence_hash"]
                ):
                    raise ExecutionRefused("skill_attempt_evidence_integrity")
            return {"events": events, "attempts": attempts, "self_report_is_not_benefit": True}

    @staticmethod
    def revoke_dependencies(c, user, episode_ids):
        if c.execute("SELECT to_regclass('public.agent_skill_sources') AS name").fetchone()["name"] is None:
            return
        rows = c.execute(
            "SELECT DISTINCT s.skill_id,s.revision FROM public.agent_skill_sources s JOIN public.agent_skills f USING(skill_id) WHERE f.user_id=%s AND s.episode_id=ANY(%s)",
            (user, episode_ids),
        ).fetchall()
        for row in rows:
            c.execute(
                "UPDATE public.agent_skill_revisions SET state='REVOKED' WHERE skill_id=%s AND revision=%s",
                (row["skill_id"], row["revision"]),
            )
            c.execute(
                "UPDATE public.agent_skills SET enabled_revision=NULL WHERE skill_id=%s AND enabled_revision=%s",
                (row["skill_id"], row["revision"]),
            )
        if rows:
            SkillStore._event(
                c,
                user,
                "dependency_revoked",
                {"revisions": [{"skill_id": str(r["skill_id"]), "revision": r["revision"]} for r in rows]},
            )
