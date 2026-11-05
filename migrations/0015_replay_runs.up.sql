CREATE TABLE IF NOT EXISTS public.agent_replay_runs (
    replay_run_id uuid PRIMARY KEY,
    source_run_id uuid NOT NULL,
    mode text NOT NULL CHECK (mode IN ('state_replay','observation_replay','shadow_replay')),
    authorization_revision bigint NOT NULL CHECK (authorization_revision > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
