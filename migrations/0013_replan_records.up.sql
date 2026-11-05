CREATE TABLE IF NOT EXISTS public.agent_replan_records (
    trigger_event_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    policy_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
