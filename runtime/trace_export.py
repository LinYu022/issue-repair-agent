"""Stable JSON trace export for the workbench and offline evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from runtime.run_log import RunLog, RunProjector


def export_trace(log: RunLog, run_id: UUID, output: Path | None = None) -> dict[str, Any]:
    events = log.events(run_id)
    if not events:
        raise ValueError("trace_run_not_found")
    state = RunProjector().project(events)
    trace = {
        "run_id": str(run_id),
        "capability_snapshot": str(events[0].capability_snapshot_id) if events[0].capability_snapshot_id else None,
        "events": [event.model_dump(mode="json") for event in events],
        "projected_state": state.model_dump(mode="json"),
        "replans": list(state.replans),
        "context_compactions": [event.payload for event in events if event.event_type == "ContextCompacted"],
        "replay_runs": [],
        "unknown_events": [event.model_dump(mode="json") for event in events if event.status == "unknown"],
    }
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return trace
