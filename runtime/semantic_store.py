"""PostgreSQL semantic memory with tenant serialization, leased candidate jobs and current-state reads."""

from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid5

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from domain.execution import ExecutionRefused
from domain.plans import document_hash
from domain.semantic_memory import SemanticDocument, SemanticSource
from runtime.execution_store import SQL_ROOT, migrate_execution
from runtime.semantic_rules import applies, extract, overlaps, terms


def migrate_semantic_memory(connection, direction="up"):
    if direction not in {"up", "down", "status"}:
        raise ExecutionRefused("semantic_migration_direction")
    checksum = document_hash(
        {k: (SQL_ROOT / f"0005_semantic_memory.{k}.sql").read_text(encoding="utf-8") for k in ("up", "down")}
    )
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(2026102813)")
        if migrate_execution(connection, "status") != 1:
            raise ExecutionRefused("semantic_memory_requires_execution_schema")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS public.agent_memory_schema(version integer PRIMARY KEY,checksum text NOT NULL)"
        )
        rows = connection.execute("SELECT version,checksum FROM public.agent_memory_schema").fetchall()
        if rows and rows != [(1, checksum)]:
            raise ExecutionRefused("semantic_memory_migration_checksum")
        if direction == "up" and not rows:
            connection.execute((SQL_ROOT / "0005_semantic_memory.up.sql").read_text())
            connection.execute("INSERT INTO public.agent_memory_schema VALUES(1,%s)", (checksum,))
            return 1
        if direction == "down" and rows:
            if connection.execute("SELECT to_regclass('public.agent_episodes')").fetchone()[0] is not None:
                raise ExecutionRefused("episodic_schema_must_be_removed_first")
            connection.execute("LOCK TABLE public.agent_memory_namespaces IN ACCESS EXCLUSIVE MODE")
            if connection.execute("SELECT EXISTS(SELECT 1 FROM public.agent_memory_namespaces)").fetchone()[0]:
                raise ExecutionRefused("semantic_memory_downgrade_data_exists")
            connection.execute((SQL_ROOT / "0005_semantic_memory.down.sql").read_text())
            connection.execute("DELETE FROM public.agent_memory_schema")
            return 0
        return 1 if rows else 0


class SemanticStore:
    def __init__(self, execution):
        self.execution = execution

    @contextmanager
    def transaction(self, user_id):
        with self.execution.connection() as c:
            c.row_factory = dict_row
            c.execute(
                "INSERT INTO public.agent_memory_namespaces(user_id) VALUES(%s) ON CONFLICT DO NOTHING", (user_id,)
            )
            namespace = c.execute(
                "SELECT * FROM public.agent_memory_namespaces WHERE user_id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            yield c, namespace

    @staticmethod
    def _source(c, user_id, source_id):
        row = c.execute(
            "SELECT * FROM public.agent_memory_sources WHERE user_id=%s AND source_id=%s", (user_id, source_id)
        ).fetchone()
        if row is None:
            raise ExecutionRefused("semantic_source_not_found")
        value = SemanticSource.model_validate(row["payload"])
        if document_hash(value) != row["payload_hash"] or (value.user_id, value.source_id, value.scope.repo) != (
            user_id,
            source_id,
            row["repo"],
        ):
            raise ExecutionRefused("semantic_source_integrity")
        return row, value

    @staticmethod
    def _entry(c, user_id, memory_id):
        row = c.execute(
            "SELECT m.*,r.document,r.document_hash AS revision_hash,r.created_at AS revision_created_at FROM public.agent_semantic_memories m JOIN public.agent_memory_revisions r ON r.memory_id=m.memory_id AND r.revision=m.current_revision WHERE m.user_id=%s AND m.memory_id=%s",
            (user_id, memory_id),
        ).fetchone()
        if row is None:
            raise ExecutionRefused("semantic_memory_not_found")
        doc = SemanticDocument.model_validate(row["document"])
        if (
            row["revision_hash"] != row["current_hash"]
            or document_hash(doc) != row["current_hash"]
            or (doc.memory_id, doc.user_id, doc.revision, doc.scope.repo)
            != (
                memory_id,
                user_id,
                row["current_revision"],
                row["repo"],
            )
        ):
            raise ExecutionRefused("semantic_memory_integrity")
        return row, doc

    @staticmethod
    def _bump(c, user_id):
        c.execute("UPDATE public.agent_memory_namespaces SET epoch=epoch+1 WHERE user_id=%s", (user_id,))
        c.execute("DELETE FROM public.agent_memory_cache WHERE user_id=%s", (user_id,))

    @staticmethod
    def _event(c, user_id, kind, details, request_id=None, input_hash=None):
        payload = {"kind": kind, "details": details, "input_hash": input_hash}
        c.execute(
            "INSERT INTO public.agent_memory_events(user_id,sequence,request_id,payload,payload_hash) SELECT %s,COALESCE(MAX(sequence),0)+1,%s,%s,%s FROM public.agent_memory_events WHERE user_id=%s",
            (user_id, request_id, Jsonb(payload), document_hash(payload), user_id),
        )

    @staticmethod
    def _seen(c, user_id, request_id, value):
        row = c.execute(
            "SELECT payload,payload_hash FROM public.agent_memory_events WHERE user_id=%s AND request_id=%s",
            (user_id, request_id),
        ).fetchone()
        if row:
            if document_hash(row["payload"]) != row["payload_hash"] or row["payload"]["input_hash"] != document_hash(
                value
            ):
                raise ExecutionRefused("semantic_request_id_payload_conflict")
            return row["payload"]["details"]
        return None

    @staticmethod
    def _view(row, doc):
        return {
            "memory_id": str(doc.memory_id),
            "revision": doc.revision,
            "document_hash": row["current_hash"],
            "state": row["state"],
            "document": doc.model_dump(mode="json"),
            "available_at": row["available_at"].isoformat(),
        }

    def register_source(self, source: SemanticSource):
        """Trusted ingestion only. HTTP/model callers cannot label arbitrary payloads as trusted sources."""
        with self.transaction(source.user_id) as (c, _):
            old = c.execute(
                "SELECT payload_hash FROM public.agent_memory_sources WHERE source_id=%s", (source.source_id,)
            ).fetchone()
            if old:
                if old["payload_hash"] != document_hash(source):
                    raise ExecutionRefused("semantic_source_id_conflict")
                return source.source_id
            for parent in source.dependencies:
                row, doc = self._entry(c, source.user_id, parent.memory_id)
                self._expect(row, doc, parent.revision, parent.document_hash)
                if row["state"] != "ACTIVE" or source.scope != doc.scope:
                    raise ExecutionRefused("semantic_parent_inactive_or_outside_scope")
            c.execute(
                "INSERT INTO public.agent_memory_sources(source_id,user_id,repo,payload,payload_hash,status) VALUES(%s,%s,%s,%s,%s,'LIVE')",
                (
                    source.source_id,
                    source.user_id,
                    source.scope.repo,
                    Jsonb(source.model_dump(mode="json")),
                    document_hash(source),
                ),
            )
            self._event(
                c, source.user_id, "source_registered", {"source_id": str(source.source_id), "origin": source.origin}
            )
        return source.source_id

    def enqueue(self, user_id, source_id):
        with self.transaction(user_id) as (c, _):
            row, _ = self._source(c, user_id, source_id)
            if row["status"] != "LIVE":
                raise ExecutionRefused("semantic_source_revoked")
            job_id = uuid5(source_id, "semantic-candidate-job-v1")
            c.execute(
                "INSERT INTO public.agent_memory_jobs(job_id,source_id,user_id,state) VALUES(%s,%s,%s,'PENDING') ON CONFLICT(source_id) DO NOTHING",
                (job_id, source_id, user_id),
            )
            return job_id

    def read_source(self, user_id, source_id):
        with self.transaction(user_id) as (c, _):
            row, source = self._source(c, user_id, source_id)
            return {
                "source": source.model_dump(mode="json"),
                "status": row["status"],
                "source_hash": row["payload_hash"],
                "available_at": row["available_at"].isoformat(),
            }

    def claim(self, worker_id, *, seconds=15, job_id=None):
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise ExecutionRefused("semantic_bounded_job_lease_required")
        with self.execution.connection() as c:
            c.row_factory = dict_row
            row = c.execute(
                "SELECT * FROM public.agent_memory_jobs WHERE (%s::uuid IS NULL OR job_id=%s) AND (state='PENDING' OR (state='RUNNING' AND lease_until<=clock_timestamp())) ORDER BY created_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1",
                (job_id, job_id),
            ).fetchone()
            if row is None:
                return None
            c.execute(
                "UPDATE public.agent_memory_jobs SET state='RUNNING',worker_id=%s,lease_epoch=lease_epoch+1,lease_until=clock_timestamp()+%s*interval '1 second',attempts=attempts+1 WHERE job_id=%s",
                (worker_id, seconds, row["job_id"]),
            )
            return {
                "job_id": row["job_id"],
                "user_id": row["user_id"],
                "worker_id": worker_id,
                "lease_epoch": row["lease_epoch"] + 1,
            }

    def finish_candidate(self, lease, *, fault=None):
        with self.transaction(lease["user_id"]) as (c, _):
            row = c.execute(
                "SELECT *,lease_until>clock_timestamp() AS lease_live FROM public.agent_memory_jobs WHERE job_id=%s AND user_id=%s FOR UPDATE",
                (lease["job_id"], lease["user_id"]),
            ).fetchone()
            if (
                row is None
                or row["state"] != "RUNNING"
                or row["worker_id"] != lease["worker_id"]
                or row["lease_epoch"] != lease["lease_epoch"]
                or not row["lease_live"]
            ):
                raise ExecutionRefused("semantic_stale_job_worker")
            source_row, source = self._source(c, lease["user_id"], row["source_id"])
            if source_row["status"] != "LIVE":
                raise ExecutionRefused("semantic_source_revoked")
            memory_id = uuid5(row["source_id"], "semantic-fact-v1")
            doc = extract(source, memory_id)
            self._insert(c, doc)
            if fault:
                fault("candidate_inserted_before_commit")
            c.execute(
                "UPDATE public.agent_memory_jobs SET state='DONE',memory_id=%s,lease_until=NULL WHERE job_id=%s",
                (memory_id, row["job_id"]),
            )
            self._event(
                c,
                source.user_id,
                "candidate_extracted",
                {
                    "memory_id": str(memory_id),
                    "source_id": str(source.source_id),
                    "worker_id": str(lease["worker_id"]),
                    "lease_epoch": lease["lease_epoch"],
                    "state": "CANDIDATE",
                    "model_calls": 0,
                },
            )
            return self._view(*self._entry(c, source.user_id, memory_id))

    def _insert(self, c, doc):
        c.execute(
            "INSERT INTO public.agent_semantic_memories(memory_id,user_id,repo,state,current_revision,current_hash) VALUES(%s,%s,%s,'CANDIDATE',%s,%s)",
            (doc.memory_id, doc.user_id, doc.scope.repo, doc.revision, document_hash(doc)),
        )
        self._revision(c, doc)
        for parent in doc.dependencies:
            c.execute(
                "INSERT INTO public.agent_memory_dependencies VALUES(%s,%s,%s,%s)",
                (parent.memory_id, doc.memory_id, parent.revision, parent.document_hash),
            )

    @staticmethod
    def _revision(c, doc):
        c.execute(
            "INSERT INTO public.agent_memory_revisions(memory_id,revision,document,document_hash) VALUES(%s,%s,%s,%s)",
            (doc.memory_id, doc.revision, Jsonb(doc.model_dump(mode="json")), document_hash(doc)),
        )

    @staticmethod
    def _expect(row, doc, revision, expected_hash):
        if (doc.revision, row["current_hash"]) != (revision, expected_hash):
            raise ExecutionRefused("semantic_revision_conflict")

    def read(self, user_id, memory_id):
        with self.transaction(user_id) as (c, _):
            return self._view(*self._entry(c, user_id, memory_id))

    def candidates(self, user_id):
        with self.transaction(user_id) as (c, _):
            rows = c.execute(
                "SELECT memory_id FROM public.agent_semantic_memories WHERE user_id=%s AND state IN ('CANDIDATE','CONFLICT','STALE') ORDER BY available_at,memory_id LIMIT 100",
                (user_id,),
            ).fetchall()
            return tuple(self._view(*self._entry(c, user_id, r["memory_id"])) for r in rows)

    def _validate(self, c, doc):
        row, source = self._source(c, doc.user_id, doc.source_id)
        if row["status"] != "LIVE" or source.origin == "model_inference":
            raise ExecutionRefused("semantic_source_unverified_or_revoked")
        if extract(source, doc.memory_id, doc.revision) != doc:
            raise ExecutionRefused("semantic_claim_not_supported_by_source")
        evidence = source.evidence
        expected = {
            "user_confirmation": "authenticated_owner_confirmation",
            "static_document": "descriptor_verified_quote",
            "visible_execution": "verified_visible_tool_and_raw_streams",
        }[source.origin]
        if evidence.get("verification") != expected:
            raise ExecutionRefused("semantic_source_validation_missing")
        if source.origin == "visible_execution" and (
            not source.scope.environment_hash
            or not source.scope.workspace_version
            or evidence.get("exit_code") != 0
            or not evidence.get("cleanup_confirmed")
        ):
            raise ExecutionRefused("semantic_command_requires_exact_environment_and_success")
        for parent in doc.dependencies:
            parent_row, parent_doc = self._entry(c, doc.user_id, parent.memory_id)
            self._expect(parent_row, parent_doc, parent.revision, parent.document_hash)
            if parent_row["state"] != "ACTIVE":
                raise ExecutionRefused("semantic_parent_not_active")

    def _deactivate(self, c, user_id, memory_id, state):
        rows = c.execute(
            "WITH RECURSIVE descendants(memory_id) AS (SELECT memory_id FROM public.agent_semantic_memories WHERE memory_id=%s AND user_id=%s UNION SELECT d.child_id FROM public.agent_memory_dependencies d JOIN descendants p ON d.parent_id=p.memory_id) SELECT memory_id FROM descendants",
            (memory_id, user_id),
        ).fetchall()
        ids = [r["memory_id"] for r in rows]
        if ids:
            if c.execute("SELECT to_regclass('public.agent_episodes')").fetchone()["to_regclass"] is not None:
                episodes = c.execute(
                    "SELECT episode_id FROM public.agent_episode_dependencies WHERE parent_id=ANY(%s)", (ids,)
                ).fetchall()
                episode_ids = [e["episode_id"] for e in episodes]
                if episode_ids:
                    from runtime.episode_store import EpisodeStore

                    EpisodeStore.revoke_closure(c, user_id, episode_ids)
            c.execute(
                "UPDATE public.agent_semantic_memories SET state=CASE WHEN memory_id=%s THEN %s WHEN state IN ('REVOKED','SUPERSEDED') THEN state ELSE 'STALE' END,updated_at=clock_timestamp() WHERE user_id=%s AND memory_id=ANY(%s)",
                (memory_id, state, user_id, ids),
            )
            c.execute("DELETE FROM public.agent_memory_active_index WHERE memory_id=ANY(%s)", (ids,))
        return [str(i) for i in ids]

    def review(self, user_id, request):
        with self.transaction(user_id) as (c, _):
            previous = self._seen(c, user_id, request.request_id, request)
            if previous is not None:
                return previous
            row, doc = self._entry(c, user_id, request.memory_id)
            self._expect(row, doc, request.expected_revision, request.expected_hash)
            affected = []
            if request.action == "revoke":
                affected = self._deactivate(c, user_id, doc.memory_id, "REVOKED")
            else:
                if row["state"] not in {"CANDIDATE", "CONFLICT"}:
                    raise ExecutionRefused("semantic_new_candidate_required_for_revalidation")
                self._validate(c, doc)
                for ref in request.supersedes:
                    if ref.memory_id == doc.memory_id:
                        raise ExecutionRefused("semantic_self_supersede_refused")
                    old, other = self._entry(c, user_id, ref.memory_id)
                    self._expect(old, other, ref.revision, ref.document_hash)
                    if (
                        other.key != doc.key
                        or not overlaps(doc.scope, other.scope)
                        or old["state"] not in {"ACTIVE", "CONFLICT"}
                    ):
                        raise ExecutionRefused("semantic_supersedes_wrong_claim")
                    affected.extend(self._deactivate(c, user_id, other.memory_id, "SUPERSEDED"))
                self._validate(c, doc)
                conflicts = []
                for item in c.execute(
                    "SELECT memory_id FROM public.agent_semantic_memories WHERE user_id=%s AND repo=%s AND state IN ('ACTIVE','CONFLICT') AND memory_id<>%s",
                    (user_id, doc.scope.repo, doc.memory_id),
                ).fetchall():
                    _, other = self._entry(c, user_id, item["memory_id"])
                    if other.key == doc.key and other.value != doc.value and overlaps(other.scope, doc.scope):
                        conflicts.append(other.memory_id)
                if conflicts:
                    for mid in [doc.memory_id, *conflicts]:
                        affected.extend(self._deactivate(c, user_id, mid, "CONFLICT"))
                else:
                    c.execute(
                        "UPDATE public.agent_semantic_memories SET state='ACTIVE',available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE memory_id=%s",
                        (doc.memory_id,),
                    )
                    c.execute(
                        "INSERT INTO public.agent_memory_active_index VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(memory_id) DO UPDATE SET revision=EXCLUDED.revision,document_hash=EXCLUDED.document_hash,terms=EXCLUDED.terms",
                        (
                            doc.memory_id,
                            user_id,
                            doc.scope.repo,
                            doc.revision,
                            document_hash(doc),
                            " ".join(sorted(terms(doc.key + " " + doc.value))),
                        ),
                    )
            self._bump(c, user_id)
            current, _ = self._entry(c, user_id, doc.memory_id)
            result = {"memory_id": str(doc.memory_id), "state": current["state"], "affected": affected}
            self._event(c, user_id, "memory_" + request.action, result, request.request_id, document_hash(request))
            return result

    def amend(self, user_id, memory_id, request):
        with self.transaction(user_id) as (c, _):
            identity = {"memory_id": str(memory_id), "request": request.model_dump(mode="json")}
            previous = self._seen(c, user_id, request.request_id, identity)
            if previous is not None:
                return previous
            row, doc = self._entry(c, user_id, memory_id)
            self._expect(row, doc, request.expected_revision, request.expected_hash)
            if row["state"] in {"REVOKED", "SUPERSEDED"}:
                raise ExecutionRefused("semantic_revoked_memory_cannot_be_amended")
            source_row, source = self._source(c, user_id, request.source_id)
            if source_row["status"] != "LIVE" or source.scope.repo != doc.scope.repo or source.key != doc.key:
                raise ExecutionRefused("semantic_amend_source_scope_mismatch")
            if any(p.memory_id == memory_id for p in source.dependencies):
                raise ExecutionRefused("semantic_dependency_cycle")
            descendants = c.execute(
                "WITH RECURSIVE children(id) AS (SELECT child_id FROM public.agent_memory_dependencies WHERE parent_id=%s UNION SELECT d.child_id FROM public.agent_memory_dependencies d JOIN children c ON d.parent_id=c.id) SELECT id FROM children",
                (memory_id,),
            ).fetchall()
            if any(p.memory_id in {r["id"] for r in descendants} for p in source.dependencies):
                raise ExecutionRefused("semantic_dependency_cycle")
            self._deactivate(c, user_id, memory_id, "STALE")
            revised = extract(source, memory_id, doc.revision + 1)
            self._revision(c, revised)
            c.execute("DELETE FROM public.agent_memory_dependencies WHERE child_id=%s", (memory_id,))
            for parent in revised.dependencies:
                c.execute(
                    "INSERT INTO public.agent_memory_dependencies VALUES(%s,%s,%s,%s)",
                    (parent.memory_id, memory_id, parent.revision, parent.document_hash),
                )
            c.execute(
                "UPDATE public.agent_semantic_memories SET current_revision=%s,current_hash=%s,state='CANDIDATE',available_at=clock_timestamp(),updated_at=clock_timestamp() WHERE memory_id=%s",
                (revised.revision, document_hash(revised), memory_id),
            )
            self._bump(c, user_id)
            result = {
                "memory_id": str(memory_id),
                "state": "CANDIDATE",
                "revision": revised.revision,
                "document_hash": document_hash(revised),
            }
            self._event(c, user_id, "memory_amended", result, request.request_id, document_hash(identity))
            return result

    def revoke_source(self, user_id, source_id, request_id, reason):
        value = {"source_id": str(source_id), "reason": reason}
        with self.transaction(user_id) as (c, _):
            previous = self._seen(c, user_id, request_id, value)
            if previous is not None:
                return previous
            self._source(c, user_id, source_id)
            c.execute(
                "UPDATE public.agent_memory_sources SET status='REVOKED' WHERE user_id=%s AND source_id=%s",
                (user_id, source_id),
            )
            affected = []
            for row in c.execute(
                "SELECT memory_id FROM public.agent_memory_revisions WHERE document->>'source_id'=%s", (str(source_id),)
            ).fetchall():
                owner, doc = self._entry(c, user_id, row["memory_id"])
                if doc.source_id == source_id:
                    affected.extend(self._deactivate(c, user_id, row["memory_id"], "REVOKED"))
            c.execute(
                "UPDATE public.agent_memory_jobs SET state='CANCELLED',lease_until=NULL WHERE user_id=%s AND source_id=%s AND state IN ('PENDING','RUNNING')",
                (user_id, source_id),
            )
            self._bump(c, user_id)
            result = {
                "source_id": str(source_id),
                "state": "REVOKED",
                "affected": sorted(set(affected)),
                "history_policy": "retained_with_revoked_marker_not_retrievable",
            }
            self._event(c, user_id, "source_revoked", result, request_id, document_hash(value))
            return result

    def invalidate(self, user_id, change):
        with self.transaction(user_id) as (c, _):
            previous = self._seen(c, user_id, change.request_id, change)
            if previous is not None:
                return previous
            affected = []
            sources = c.execute(
                "SELECT source_id FROM public.agent_memory_sources WHERE user_id=%s AND repo=%s AND status='LIVE'",
                (user_id, change.repo),
            ).fetchall()
            for item in sources:
                _, source = self._source(c, user_id, item["source_id"])
                scope = source.scope
                if change.branch in scope.branches and (
                    any(p in scope.watched_files or scope.workspace_version is not None for p in change.changed_paths)
                    or change.old_environment_hash is not None
                    and scope.environment_hash == change.old_environment_hash
                ):
                    c.execute(
                        "UPDATE public.agent_memory_sources SET status='INVALIDATED' WHERE source_id=%s",
                        (source.source_id,),
                    )
                    c.execute(
                        "UPDATE public.agent_memory_jobs SET state='CANCELLED',lease_until=NULL WHERE source_id=%s AND state IN ('PENDING','RUNNING')",
                        (source.source_id,),
                    )
            rows = c.execute(
                "SELECT memory_id FROM public.agent_semantic_memories WHERE user_id=%s AND repo=%s AND state IN ('ACTIVE','CANDIDATE','CONFLICT')",
                (user_id, change.repo),
            ).fetchall()
            for item in rows:
                _, doc = self._entry(c, user_id, item["memory_id"])
                scope = doc.scope
                if change.branch in scope.branches and (
                    any(p in scope.watched_files or scope.workspace_version is not None for p in change.changed_paths)
                    or change.old_environment_hash is not None
                    and scope.environment_hash == change.old_environment_hash
                ):
                    affected.extend(self._deactivate(c, user_id, doc.memory_id, "STALE"))
            if c.execute("SELECT to_regclass('public.agent_episodes')").fetchone()["to_regclass"] is not None:
                from domain.episodic_memory import EpisodeDocument
                from runtime.episode_store import EpisodeStore

                episodes = c.execute(
                    "SELECT episode_id,document,document_hash FROM public.agent_episodes WHERE user_id=%s AND repo=%s AND state='ACTIVE'",
                    (user_id, change.repo),
                ).fetchall()
                invalid = []
                for episode in episodes:
                    doc = EpisodeDocument.model_validate(episode["document"])
                    if document_hash(doc) != episode["document_hash"]:
                        raise ExecutionRefused("episode_invalidation_source_integrity")
                    if change.branch in doc.scope.branches and (
                        any(p in doc.scope.watched_files for p in change.changed_paths)
                        or change.old_environment_hash is not None
                        and doc.scope.environment_hash == change.old_environment_hash
                    ):
                        invalid.append(episode["episode_id"])
                if invalid:
                    EpisodeStore.revoke_closure(c, user_id, invalid)
                    affected.extend(str(key) for key in invalid)
            self._bump(c, user_id)
            result = {"state": "STALE", "affected": sorted(set(affected))}
            self._event(c, user_id, "scope_invalidated", result, change.request_id, document_hash(change))
            return result

    def epoch(self, user_id):
        with self.transaction(user_id) as (_, row):
            return row["epoch"]

    def retrieve(self, user_id, query):
        if query.as_of > datetime.now(timezone.utc):
            raise ExecutionRefused("semantic_future_query_refused")
        with self.transaction(user_id) as (c, namespace):
            cache_key = document_hash(query)
            cached = c.execute(
                "SELECT items FROM public.agent_memory_cache WHERE user_id=%s AND query_hash=%s AND epoch=%s",
                (user_id, cache_key, namespace["epoch"]),
            ).fetchone()
            # Cache holds only references. Authoritative state/time/scope is rechecked on every hit.
            rows = c.execute(
                "SELECT i.memory_id,i.revision,i.document_hash,i.terms FROM public.agent_memory_active_index i JOIN public.agent_semantic_memories m ON m.memory_id=i.memory_id WHERE i.user_id=%s AND i.repo=%s AND m.state='ACTIVE' AND m.available_at<=%s",
                (user_id, query.repo, query.as_of),
            ).fetchall()
            result = []
            for indexed in rows:
                row, doc = self._entry(c, user_id, indexed["memory_id"])
                if (doc.revision, row["current_hash"]) != (indexed["revision"], indexed["document_hash"]):
                    raise ExecutionRefused("semantic_derived_index_stale")
                source_row, _ = self._source(c, user_id, doc.source_id)
                if (
                    source_row["status"] != "LIVE"
                    or source_row["available_at"] > query.as_of
                    or not applies(doc, query)
                ):
                    continue
                self._validate(c, doc)
                score = len(terms(query.text) & terms(indexed["terms"]))
                result.append((score, self._view(row, doc)))
            result.sort(key=lambda pair: (-pair[0], pair[1]["memory_id"]))
            items = tuple(value for _, value in result[: query.limit])
            refs = [
                {"memory_id": i["memory_id"], "revision": i["revision"], "document_hash": i["document_hash"]}
                for i in items
            ]
            c.execute(
                "INSERT INTO public.agent_memory_cache VALUES(%s,%s,%s,%s) ON CONFLICT(user_id,query_hash) DO UPDATE SET epoch=EXCLUDED.epoch,items=EXCLUDED.items",
                (user_id, cache_key, namespace["epoch"], Jsonb(refs)),
            )
            self._event(
                c,
                user_id,
                "semantic_retrieved",
                {
                    "task_id": str(query.task_id),
                    "query_hash": cache_key,
                    "epoch": namespace["epoch"],
                    "items": refs,
                    "cache_hit": cached is not None,
                },
            )
            return {"epoch": namespace["epoch"], "items": items, "cache_hit": cached is not None}
