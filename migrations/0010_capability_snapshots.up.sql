CREATE TABLE IF NOT EXISTS public.agent_capability_snapshots (
    snapshot_id uuid PRIMARY KEY,
    snapshot_hash text NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
