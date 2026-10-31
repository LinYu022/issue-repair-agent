CREATE TABLE public.agent_checkpoints (
    checkpoint_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES public.agent_execution_tasks(task_id),
    call_id uuid NOT NULL,
    lease_epoch bigint NOT NULL,
    execution_epoch bigint NOT NULL,
    store_path text NOT NULL,
    state text NOT NULL CHECK (state IN ('PREPARING','COMMITTED','ABORTED')),
    boundary jsonb NOT NULL,
    document jsonb,
    document_hash text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    committed_at timestamptz,
    FOREIGN KEY (task_id, call_id) REFERENCES public.agent_execution_calls(task_id, call_id),
    CHECK ((state = 'COMMITTED') = (document IS NOT NULL AND document_hash IS NOT NULL AND committed_at IS NOT NULL))
);
