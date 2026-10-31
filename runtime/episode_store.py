"""Tenant-scoped history and anchored reads, sharing the semantic-memory revocation epoch."""

import json
from datetime import datetime, timezone
from uuid import UUID

from psycopg.types.json import Jsonb

from domain.episodic_memory import EpisodeDocument
from domain.execution import ExecutionRefused
from domain.plans import document_hash
from runtime.execution_store import SQL_ROOT
from runtime.semantic_rules import applies, terms
from runtime.semantic_store import SemanticStore, migrate_semantic_memory
from tools.workspace_files import sha


def migrate_episodic_memory(connection, direction="up"):
    if direction not in {"up", "down", "status"}:
        raise ExecutionRefused("episodic_migration_direction")
    checksum = document_hash(
        {k: (SQL_ROOT / f"0006_episodic_memory.{k}.sql").read_text(encoding="utf-8") for k in ("up", "down")}
    )
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(2026102814)")
        if migrate_semantic_memory(connection, "status") != 1:
            raise ExecutionRefused("episodic_requires_semantic_schema")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS public.agent_episode_schema(version integer PRIMARY KEY,checksum text NOT NULL)"
        )
        rows = connection.execute("SELECT version,checksum FROM public.agent_episode_schema").fetchall()
        if rows and rows != [(1, checksum)]:
            raise ExecutionRefused("episodic_migration_checksum")
        if direction == "up" and not rows:
            connection.execute((SQL_ROOT / "0006_episodic_memory.up.sql").read_text())
            connection.execute("INSERT INTO public.agent_episode_schema VALUES(1,%s)", (checksum,))
            return 1
        if direction == "down" and rows:
            if connection.execute("SELECT to_regclass('public.agent_skills')").fetchone()[0] is not None:
                raise ExecutionRefused("procedural_schema_must_be_removed_first")
            connection.execute("LOCK TABLE public.agent_episodes IN ACCESS EXCLUSIVE MODE")
            if connection.execute("SELECT EXISTS(SELECT 1 FROM public.agent_episodes)").fetchone()[0]:
                raise ExecutionRefused("episodic_downgrade_data_exists")
            connection.execute((SQL_ROOT / "0006_episodic_memory.down.sql").read_text())
            connection.execute("DELETE FROM public.agent_episode_schema")
            return 0
        return 1 if rows else 0


class EpisodeStore:
    def __init__(self, execution):
        self.execution = execution
        self.semantic = SemanticStore(execution)

    @staticmethod
    def _entry(c, user_id, episode_id):
        row = c.execute(
            "SELECT * FROM public.agent_episodes WHERE user_id=%s AND episode_id=%s", (user_id, episode_id)
        ).fetchone()
        if row is None:
            raise ExecutionRefused("episode_not_found")
        doc = EpisodeDocument.model_validate(row["document"])
        if document_hash(doc) != row["document_hash"] or (
            doc.episode_id,
            doc.user_id,
            doc.source_task_id,
            doc.scope.repo,
        ) != (episode_id, user_id, row["source_task_id"], row["repo"]):
            raise ExecutionRefused("episode_document_integrity")
        return row, doc

    @staticmethod
    def _usage(c, user_id, task_id, episode_id, kind, payload, request_id=None):
        if request_id is not None:
            old = c.execute(
                "SELECT kind,payload,payload_hash FROM public.agent_episode_usage WHERE user_id=%s AND request_id=%s",
                (user_id, request_id),
            ).fetchone()
            if old:
                if old["kind"] != kind or old["payload_hash"] != document_hash(payload) or old["payload"] != payload:
                    raise ExecutionRefused("episode_usage_id_conflict")
                return
        c.execute(
            "INSERT INTO public.agent_episode_usage(user_id,sequence,consumer_task_id,episode_id,request_id,kind,payload,payload_hash) SELECT %s,COALESCE(MAX(sequence),0)+1,%s,%s,%s,%s,%s,%s FROM public.agent_episode_usage WHERE user_id=%s",
            (user_id, task_id, episode_id, request_id, kind, Jsonb(payload), document_hash(payload), user_id),
        )

    def publish(self, doc, blobs, *, fault=None):
        if set(blobs) != {str(e.anchor_id) for e in doc.events}:
            raise ExecutionRefused("episode_blob_inventory")
        if sum(len(text.encode()) for text in blobs.values()) > 2 * 1024 * 1024:
            raise ExecutionRefused("episode_total_text_limit")
        with self.semantic.transaction(doc.user_id) as (c, _):
            previous = c.execute(
                "SELECT episode_id,document_hash,state FROM public.agent_episodes WHERE user_id=%s AND source_task_id=%s",
                (doc.user_id, doc.source_task_id),
            ).fetchone()
            if previous:
                if previous["document_hash"] != document_hash(doc) or previous["episode_id"] != doc.episode_id:
                    raise ExecutionRefused("episode_import_identity_conflict")
                return {
                    "episode_id": str(doc.episode_id),
                    "document_hash": document_hash(doc),
                    "state": previous["state"],
                    "reused": True,
                }
            for parent in doc.semantic_dependencies:
                row, parent_doc = self.semantic._entry(c, doc.user_id, parent.memory_id)
                if row["state"] != "ACTIVE" or (parent_doc.revision, row["current_hash"]) != (
                    parent.revision,
                    parent.document_hash,
                ):
                    raise ExecutionRefused("episode_semantic_dependency_inactive")
            for parent in doc.parent_episodes:
                row, _ = self._entry(c, doc.user_id, parent.episode_id)
                if row["state"] != "ACTIVE" or row["document_hash"] != parent.document_hash:
                    raise ExecutionRefused("episode_parent_inactive")
            c.execute(
                "INSERT INTO public.agent_episodes(episode_id,user_id,source_task_id,repo,document,document_hash,state) VALUES(%s,%s,%s,%s,%s,%s,'ACTIVE')",
                (
                    doc.episode_id,
                    doc.user_id,
                    doc.source_task_id,
                    doc.scope.repo,
                    Jsonb(doc.model_dump(mode="json")),
                    document_hash(doc),
                ),
            )
            for event in doc.events:
                text = blobs[str(event.anchor_id)]
                data = text.encode()
                if len(data) != event.text.size_bytes or sha(data) != event.text.sha256 or len(data) > 65536:
                    raise ExecutionRefused("episode_blob_integrity")
                c.execute(
                    "INSERT INTO public.agent_episode_blobs VALUES(%s,%s,%s,%s,%s)",
                    (doc.episode_id, event.anchor_id, text, sha(data), len(data)),
                )
            for parent in doc.semantic_dependencies:
                c.execute(
                    "INSERT INTO public.agent_episode_dependencies VALUES(%s,%s,%s,%s)",
                    (doc.episode_id, parent.memory_id, parent.revision, parent.document_hash),
                )
            for parent in doc.parent_episodes:
                c.execute(
                    "INSERT INTO public.agent_episode_parents VALUES(%s,%s,%s)",
                    (doc.episode_id, parent.episode_id, parent.document_hash),
                )
            text = " ".join([doc.symptom, *doc.hypotheses, doc.outcome_reason, *[e.excerpt for e in doc.events]])
            c.execute(
                "INSERT INTO public.agent_episode_index VALUES(%s,%s,%s,%s,%s)",
                (doc.episode_id, doc.user_id, doc.scope.repo, document_hash(doc), " ".join(sorted(terms(text)))),
            )
            if fault:
                fault("episode_before_commit")
            self.semantic._bump(c, doc.user_id)
            return {
                "episode_id": str(doc.episode_id),
                "document_hash": document_hash(doc),
                "state": "ACTIVE",
                "reused": False,
            }

    def _live(self, c, row, doc, query):
        if query.as_of > datetime.now(timezone.utc):
            raise ExecutionRefused("episode_future_query_refused")
        if row["state"] != "ACTIVE" or row["available_at"] > query.as_of or not applies(doc, query):
            return False
        for parent in doc.semantic_dependencies:
            state, value = self.semantic._entry(c, doc.user_id, parent.memory_id)
            if state["state"] != "ACTIVE" or (value.revision, state["current_hash"]) != (
                parent.revision,
                parent.document_hash,
            ):
                return False
        for parent in doc.parent_episodes:
            parent_row, _ = self._entry(c, doc.user_id, parent.episode_id)
            if parent_row["state"] != "ACTIVE" or parent_row["document_hash"] != parent.document_hash:
                return False
        return True

    @staticmethod
    def summary(row, doc):
        chosen = {0, 1, len(doc.events) - 1}
        chosen.update(i for i, e in enumerate(doc.events) if e.anchor_id in doc.counterevidence)
        selected = [doc.events[i] for i in sorted(chosen)[:8]]
        return {
            "episode_id": str(doc.episode_id),
            "document_hash": row["document_hash"],
            "source_task_id": str(doc.source_task_id),
            "source_instance_id": doc.source_instance_id,
            "repo": doc.scope.repo,
            "base_commit": doc.scope.commits[0],
            "branches": list(doc.scope.branches),
            "source_initial_version": doc.source_initial_version,
            "source_final_version": doc.source_final_version,
            "symptom": doc.symptom,
            "hypotheses": list(doc.hypotheses),
            "outcome": doc.outcome,
            "outcome_reason": doc.outcome_reason,
            "failure_count": len(doc.counterevidence),
            "counterevidence_anchors": [str(a) for a in doc.counterevidence],
            "anchors": [
                {
                    "anchor_id": str(e.anchor_id),
                    "sequence": e.sequence,
                    "kind": e.kind,
                    "status": e.status,
                    "operation": e.operation,
                    "location": None if e.location is None else e.location.model_dump(mode="json"),
                }
                for e in selected
            ],
            "total_events": len(doc.events),
            "anchors_truncated": len(selected) < len(doc.events),
            "available_at": row["available_at"].isoformat(),
            "code_version_state": "historical_only_reread_current_code",
            "independent_evaluation": "not_imported",
            "grants_authority": False,
        }

    def search(self, user_id, query, *, record=True):
        if query.as_of > datetime.now(timezone.utc):
            raise ExecutionRefused("episode_future_query_refused")
        with self.semantic.transaction(user_id) as (c, _):
            rows = c.execute(
                "SELECT e.episode_id,i.document_hash AS indexed_hash,i.terms FROM public.agent_episodes e JOIN public.agent_episode_index i ON i.episode_id=e.episode_id WHERE e.user_id=%s AND i.user_id=%s AND e.repo=%s AND e.state='ACTIVE' AND e.available_at<=%s AND (%s::uuid IS NULL OR e.source_task_id=%s)",
                (user_id, user_id, query.repo, query.as_of, query.source_task_id, query.source_task_id),
            ).fetchall()
            matches = []
            for indexed in rows:
                row, doc = self._entry(c, user_id, indexed["episode_id"])
                if row["document_hash"] != indexed["indexed_hash"]:
                    raise ExecutionRefused("episode_index_integrity")
                if not self._live(c, row, doc, query):
                    continue
                score = len(terms(query.text) & terms(indexed["terms"]))
                matched = []
                if query.mode == "session":
                    for event in doc.events:
                        blob = c.execute(
                            "SELECT text FROM public.agent_episode_blobs WHERE episode_id=%s AND anchor_id=%s",
                            (doc.episode_id, event.anchor_id),
                        ).fetchone()
                        if blob is None or sha(blob["text"].encode()) != event.text.sha256:
                            raise ExecutionRefused("episode_blob_integrity")
                        overlap = len(terms(query.text) & terms(blob["text"]))
                        if not query.text.strip() or overlap:
                            matched.append(
                                {
                                    "anchor_id": str(event.anchor_id),
                                    "sequence": event.sequence,
                                    "kind": event.kind,
                                    "status": event.status,
                                    "excerpt": event.excerpt[:300],
                                    "historical_code": True,
                                }
                            )
                            score = max(score, overlap)
                if query.mode != "recent" and query.text.strip() and score == 0:
                    continue
                summary = self.summary(row, doc)
                if query.mode == "session":
                    summary["session_matches"] = matched[:8]
                matches.append((score, row["available_at"], summary))
            matches.sort(
                key=lambda v: (-v[1].timestamp(), v[2]["episode_id"])
                if query.mode == "recent"
                else (-v[0], -v[1].timestamp(), v[2]["episode_id"])
            )
            items = tuple(m[2] for m in matches[: query.limit])
            if record:
                for item in items:
                    self._usage(
                        c,
                        user_id,
                        query.task_id,
                        UUID(item["episode_id"]),
                        "recalled",
                        {
                            "document_hash": item["document_hash"],
                            "mode": query.mode,
                            "query_hash": document_hash(query),
                        },
                    )
            return items

    def _checked(self, c, user_id, query, episode_id, expected_hash):
        row, doc = self._entry(c, user_id, episode_id)
        if row["document_hash"] != expected_hash:
            raise ExecutionRefused("episode_expected_hash_changed")
        if not self._live(c, row, doc, query):
            raise ExecutionRefused("episode_no_longer_applicable")
        return row, doc

    def current_summary(self, user_id, query, episode_id, expected_hash):
        with self.semantic.transaction(user_id) as (c, _):
            return self.summary(*self._checked(c, user_id, query, episode_id, expected_hash))

    def browse(self, user_id, query, request, *, record=True):
        with self.semantic.transaction(user_id) as (c, _):
            row, doc = self._checked(c, user_id, query, request.episode_id, request.expected_hash)
            anchors = {e.anchor_id: i for i, e in enumerate(doc.events)}
            if request.anchor_id not in anchors:
                raise ExecutionRefused("episode_anchor_not_found")
            focus = anchors[request.anchor_id]
            events = doc.events[max(0, focus - request.before) : focus + request.after + 1]
            pages = []
            for event in events:
                blob = c.execute(
                    "SELECT * FROM public.agent_episode_blobs WHERE episode_id=%s AND anchor_id=%s",
                    (doc.episode_id, event.anchor_id),
                ).fetchone()
                if (
                    blob is None
                    or sha(blob["text"].encode()) != event.text.sha256
                    or len(blob["text"].encode()) != event.text.size_bytes
                    or blob["sha256"] != event.text.sha256
                    or blob["size_bytes"] != event.text.size_bytes
                ):
                    raise ExecutionRefused("episode_blob_integrity")
                offset = request.offset if event.anchor_id == request.anchor_id else 0
                if offset > len(blob["text"]):
                    raise ExecutionRefused("episode_offset_out_of_range")
                text = blob["text"][offset : offset + request.limit]
                pages.append(
                    {
                        "event": event.model_dump(mode="json"),
                        "text": text,
                        "offset": offset,
                        "offset_unit": "unicode_codepoints",
                        "next_offset": offset + len(text) if offset + len(text) < len(blob["text"]) else None,
                        "total_characters": len(blob["text"]),
                    }
                )
            result = {
                "episode": self.summary(row, doc),
                "pages": pages,
                "focus_anchor": str(request.anchor_id),
                "history_is_not_current_code": True,
            }
            if len(json.dumps(result, ensure_ascii=False).encode()) > 65536:
                raise ExecutionRefused("episode_output_limit")
            if record:
                self._usage(
                    c,
                    user_id,
                    query.task_id,
                    doc.episode_id,
                    "read",
                    {
                        "document_hash": row["document_hash"],
                        "anchor_id": str(request.anchor_id),
                        "offset": request.offset,
                        "characters": sum(len(p["text"]) for p in pages),
                    },
                )
            return result

    def assess(self, user_id, query, request, *, record=True):
        with self.semantic.transaction(user_id) as (c, _):
            row, doc = self._checked(c, user_id, query, request.episode_id, request.expected_hash)
            kind = "declared_adoption" if request.decision == "adopt" else "rejected"
            needed = "read" if request.decision == "adopt" else "recalled"
            seen = c.execute(
                "SELECT 1 FROM public.agent_episode_usage WHERE user_id=%s AND consumer_task_id=%s AND episode_id=%s AND kind=%s AND payload->>'document_hash'=%s",
                (user_id, query.task_id, doc.episode_id, needed, row["document_hash"]),
            ).fetchone()
            if not seen:
                raise ExecutionRefused("episode_assessment_requires_prior_recall_or_read")
            if record:
                self._usage(
                    c,
                    user_id,
                    query.task_id,
                    doc.episode_id,
                    kind,
                    {"document_hash": row["document_hash"], "reason": request.reason, "agent_declaration_only": True},
                )
            return {"episode_id": str(doc.episode_id), "decision": request.decision, "agent_declaration_only": True}

    def validate_refs(self, user_id, query, refs):
        with self.semantic.transaction(user_id) as (c, namespace):
            for ref in refs:
                self._checked(c, user_id, query, UUID(ref["episode_id"]), ref["document_hash"])
            return namespace["epoch"]

    def link(self, user_id, task_id, refs, epoch, call_id, record_hash, status, operation):
        with self.semantic.transaction(user_id) as (c, namespace):
            for ref in refs:
                row, _ = self._entry(c, user_id, UUID(ref["episode_id"]))
                kind = "execution_linked" if row["state"] == "ACTIVE" and namespace["epoch"] == epoch else "late_link"
                self._usage(
                    c,
                    user_id,
                    task_id,
                    UUID(ref["episode_id"]),
                    kind,
                    {
                        "document_hash": ref["document_hash"],
                        "call_id": str(call_id),
                        "call_record_hash": record_hash,
                        "status": status,
                        "operation": operation,
                        "benefit_proven": False,
                    },
                )

    def judge_analogy(self, user_id, verdict):
        with self.semantic.transaction(user_id) as (c, _):
            self._entry(c, user_id, verdict.episode_id)
            links = c.execute(
                "SELECT payload FROM public.agent_episode_usage WHERE user_id=%s AND consumer_task_id=%s AND episode_id=%s AND kind='execution_linked' AND payload->>'call_id'=%s",
                (user_id, verdict.consumer_task_id, verdict.episode_id, str(verdict.linked_call_id)),
            ).fetchall()
            if not any(link["payload"]["call_record_hash"] == verdict.call_record_hash for link in links):
                raise ExecutionRefused("episode_verdict_requires_actual_linked_call")
            self._usage(
                c,
                user_id,
                verdict.consumer_task_id,
                verdict.episode_id,
                "analogy_verdict",
                verdict.model_dump(mode="json"),
                verdict.request_id,
            )
            return {"verdict_recorded": True, "verifier": verdict.verifier}

    def withdraw(self, user_id, episode_id, request):
        with self.semantic.transaction(user_id) as (c, _):
            row, doc = self._entry(c, user_id, episode_id)
            if row["document_hash"] != request.expected_hash:
                raise ExecutionRefused("episode_expected_hash_changed")
            payload = {"document_hash": row["document_hash"], "reason": request.reason, "state": "REVOKED"}
            self._usage(c, user_id, doc.source_task_id, episode_id, "withdrawn", payload, request.request_id)
            self.revoke_closure(c, user_id, [episode_id])
            self.semantic._bump(c, user_id)
            return payload

    @staticmethod
    def revoke_closure(c, user_id, ids):
        affected = c.execute(
            "WITH RECURSIVE descendants(id) AS (SELECT episode_id FROM public.agent_episodes WHERE user_id=%s AND episode_id=ANY(%s) UNION SELECT p.child_id FROM public.agent_episode_parents p JOIN descendants d ON p.parent_id=d.id) SELECT id FROM descendants",
            (user_id, ids),
        ).fetchall()
        all_ids = [r["id"] for r in affected]
        if all_ids:
            c.execute(
                "UPDATE public.agent_episodes SET state='REVOKED' WHERE user_id=%s AND episode_id=ANY(%s)",
                (user_id, all_ids),
            )
            c.execute(
                "DELETE FROM public.agent_episode_index WHERE user_id=%s AND episode_id=ANY(%s)", (user_id, all_ids)
            )
            from runtime.skill_store import SkillStore

            SkillStore.revoke_dependencies(c, user_id, all_ids)

    def usage(self, user_id, task_id):
        with self.semantic.transaction(user_id) as (c, _):
            rows = c.execute(
                "SELECT * FROM public.agent_episode_usage WHERE user_id=%s AND consumer_task_id=%s ORDER BY sequence",
                (user_id, task_id),
            ).fetchall()
            for row in rows:
                if document_hash(row["payload"]) != row["payload_hash"]:
                    raise ExecutionRefused("episode_usage_integrity")
            counts = {
                k: sum(r["kind"] == k for r in rows)
                for k in ["recalled", "read", "declared_adoption", "rejected", "execution_linked", "late_link"]
            }
            verdicts = {
                k: sum(r["kind"] == "analogy_verdict" and r["payload"]["verdict"] == k for r in rows)
                for k in ["correct", "incorrect", "undetermined"]
            }
            return {
                "events": [
                    {
                        **r,
                        "user_id": str(r["user_id"]),
                        "consumer_task_id": str(r["consumer_task_id"]),
                        "episode_id": str(r["episode_id"]) if r["episode_id"] else None,
                        "request_id": str(r["request_id"]) if r["request_id"] else None,
                        "created_at": r["created_at"].isoformat(),
                    }
                    for r in rows
                ],
                "counts": counts,
                "visible_development_verdicts": verdicts,
                "self_declared_adoption_is_not_benefit": True,
            }
