"""Run event appenders, a projector, and a PostgreSQL-backed implementation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from domain.run_events import ProjectedRunState, RunEvent


class RunEventError(ValueError):
    pass


class RunLog:
    def __init__(self):
        self._events: dict[UUID, list[RunEvent]] = defaultdict(list)
        self._ids: dict[UUID, RunEvent] = {}

    def append(self, event: RunEvent) -> RunEvent:
        if event.event_id in self._ids:
            raise RunEventError("duplicate_event_id")
        events = self._events[event.run_id]
        expected = len(events) + 1
        if event.sequence_no != expected:
            raise RunEventError("sequence_not_contiguous")
        if expected == 1:
            if event.parent_event_id is not None:
                raise RunEventError("first_event_parent_must_be_null")
        elif event.parent_event_id != events[-1].event_id:
            raise RunEventError("event_parent_mismatch")
        if event.payload_hash is None:
            raise RunEventError("event_payload_hash_missing")
        events.append(event)
        self._ids[event.event_id] = event
        return event

    def append_many(self, events: Iterable[RunEvent]) -> None:
        for event in events:
            self.append(event)

    def append_idempotent(self, event: RunEvent) -> RunEvent:
        existing = self._ids.get(event.event_id)
        if existing is not None:
            if existing.model_dump(mode="json") != event.model_dump(mode="json"):
                raise RunEventError("event_id_payload_conflict")
            return existing
        return self.append(event)

    def events(self, run_id: UUID) -> tuple[RunEvent, ...]:
        return tuple(self._events.get(run_id, ()))

    def get(self, event_id: UUID) -> RunEvent:
        try:
            return self._ids[event_id]
        except KeyError:
            raise RunEventError("event_not_found") from None

    @classmethod
    def from_directory(cls, directory) -> "RunLog":
        log = cls()
        for path in sorted(directory.glob("*.json")):
            log.append(RunEvent.model_validate_json(path.read_text(encoding="utf-8")))
        return log


class RunProjector:
    def project(self, events: Iterable[RunEvent]) -> ProjectedRunState:
        ordered = list(events)
        if not ordered:
            raise RunEventError("cannot_project_empty_run")
        state = ProjectedRunState(run_id=ordered[0].run_id)
        for event in ordered:
            if event.run_id != state.run_id or event.sequence_no != state.last_sequence + 1:
                raise RunEventError("invalid_event_stream")
            state = self._apply(state, event)
        return state

    def _apply(self, state: ProjectedRunState, event: RunEvent) -> ProjectedRunState:
        payload = event.payload
        updates: dict[str, Any] = {"last_sequence": event.sequence_no, "last_event_id": event.event_id, "workspace_version": event.workspace_version, "authorization_revision": event.authorization_revision, "context_revision": event.context_revision}
        if event.event_type == "PlanProposed":
            updates["plan"] = payload
        elif event.event_type == "ToolObservationReceived":
            updates["observations"] = (*state.observations, payload)
        elif event.event_type == "ReplanTriggered":
            updates["replans"] = (*state.replans, payload)
        elif event.event_type == "ContextCompacted":
            updates["context_revision"] = event.context_revision
        elif event.event_type == "RunPaused":
            updates["status"] = "paused"
        elif event.event_type == "RunResumed":
            updates["status"] = "running"
        elif event.event_type == "RunStopped":
            updates["status"], updates["stop_reason"] = "stopped", payload.get("reason")
        elif event.event_type == "RunCompleted":
            updates["status"] = "completed"
        elif event.event_type == "RunCreated":
            updates["status"] = "running"
        return state.model_copy(update=updates)


class PostgresRunLog(RunLog):
    """Append-only PostgreSQL event log.  The schema is installed separately."""

    def __init__(self, dsn: str):
        super().__init__()
        if not dsn.strip():
            raise ValueError("run_event_database_dsn_required")
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn, connect_timeout=3)

    def append(self, event: RunEvent) -> RunEvent:
        with self._connect() as connection:
            try:
                row = connection.execute("SELECT sequence_no,event_id FROM public.agent_run_events WHERE run_id=%s ORDER BY sequence_no DESC LIMIT 1 FOR UPDATE", (event.run_id,)).fetchone()
                expected = 1 if row is None else row[0] + 1
                if event.sequence_no != expected:
                    raise RunEventError("sequence_not_contiguous")
                if row is None and event.parent_event_id is not None:
                    raise RunEventError("first_event_parent_must_be_null")
                if row is not None and event.parent_event_id != row[1]:
                    raise RunEventError("event_parent_mismatch")
                connection.execute(
                    "INSERT INTO public.agent_run_events (event_id,run_id,parent_event_id,sequence_no,event_type,task_version,authorization_revision,capability_snapshot_id,context_revision,workspace_version,payload_artifact_id,payload,payload_hash,occurred_at,actor,status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (event.event_id,event.run_id,event.parent_event_id,event.sequence_no,event.event_type,event.task_version,event.authorization_revision,event.capability_snapshot_id,event.context_revision,event.workspace_version,event.payload_artifact_id,Jsonb(event.payload),event.payload_hash,event.occurred_at,event.actor,event.status),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise RunEventError("duplicate_event_id_or_sequence") from exc
        return event

    def events(self, run_id: UUID) -> tuple[RunEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT event_id,run_id,parent_event_id,sequence_no,event_type,task_version,authorization_revision,capability_snapshot_id,context_revision,workspace_version,payload_artifact_id,payload,payload_hash,occurred_at,actor,status FROM public.agent_run_events WHERE run_id=%s ORDER BY sequence_no", (run_id,)).fetchall()
        return tuple(RunEvent(event_id=r[0],run_id=r[1],parent_event_id=r[2],sequence_no=r[3],event_type=r[4],task_version=r[5],authorization_revision=r[6],capability_snapshot_id=r[7],context_revision=r[8],workspace_version=r[9],payload_artifact_id=r[10],payload=r[11],payload_hash=r[12],occurred_at=r[13],actor=r[14],status=r[15]) for r in rows)
