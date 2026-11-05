CREATE TABLE IF NOT EXISTS public.agent_run_events (
    event_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    parent_event_id uuid,
    sequence_no bigint NOT NULL CHECK (sequence_no > 0),
    event_type text NOT NULL,
    task_version text NOT NULL,
    authorization_revision bigint NOT NULL CHECK (authorization_revision > 0),
    capability_snapshot_id uuid,
    context_revision bigint NOT NULL DEFAULT 0 CHECK (context_revision >= 0),
    workspace_version text NOT NULL,
    payload_artifact_id uuid,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    payload_hash text NOT NULL,
    occurred_at timestamptz NOT NULL,
    actor text NOT NULL,
    status text NOT NULL,
    UNIQUE(run_id, sequence_no),
    UNIQUE(run_id, parent_event_id)
);
CREATE INDEX IF NOT EXISTS agent_run_events_run_order ON public.agent_run_events(run_id, sequence_no);
