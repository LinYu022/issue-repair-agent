CREATE TABLE public.agent_recovery_runtime (
    task_id uuid PRIMARY KEY REFERENCES public.agent_execution_tasks(task_id),
    workspace_path text NOT NULL,
    trace_path text NOT NULL,
    checkpoint_path text NOT NULL,
    lock_path text NOT NULL,
    recovery_parent text NOT NULL,
    spec jsonb NOT NULL,
    spec_hash text NOT NULL,
    active_recovery_id uuid
);
CREATE TABLE public.agent_recovery_runs (
    recovery_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES public.agent_recovery_runtime(task_id),
    request jsonb NOT NULL,
    request_hash text NOT NULL,
    state text NOT NULL,
    review jsonb,
    review_hash text,
    decision jsonb,
    result jsonb,
    result_hash text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.agent_recovery_events (
    recovery_id uuid NOT NULL REFERENCES public.agent_recovery_runs(recovery_id),
    sequence integer NOT NULL,
    event jsonb NOT NULL,
    event_hash text NOT NULL,
    PRIMARY KEY(recovery_id,sequence)
);
