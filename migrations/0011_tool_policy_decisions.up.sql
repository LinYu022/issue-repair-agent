CREATE TABLE IF NOT EXISTS public.agent_tool_policy_decisions (
    decision_id uuid PRIMARY KEY,
    task_id uuid NOT NULL,
    snapshot_id uuid,
    policy_hash text NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
