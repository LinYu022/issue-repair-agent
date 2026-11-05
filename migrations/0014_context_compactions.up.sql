CREATE TABLE IF NOT EXISTS public.agent_context_compactions (
    compaction_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    output_context_hash text NOT NULL,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
