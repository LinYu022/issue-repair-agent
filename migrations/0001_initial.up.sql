CREATE TABLE public.agent_tasks (
    task_id uuid PRIMARY KEY,
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (payload->>'task_id' IS NOT NULL AND payload->>'task_id' = task_id::text)
);

CREATE TABLE public.agent_call_records (
    call_kind text NOT NULL CHECK (call_kind IN ('model', 'tool')),
    call_id uuid NOT NULL,
    task_id uuid NOT NULL REFERENCES public.agent_tasks(task_id),
    position integer NOT NULL CHECK (position >= 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    PRIMARY KEY (call_kind, call_id),
    UNIQUE (task_id, position),
    CHECK (payload->>'task_id' IS NOT NULL AND payload->>'task_id' = task_id::text),
    CHECK (payload->>'call_id' IS NOT NULL AND payload->>'call_id' = call_id::text),
    CHECK (payload->>'call_kind' IS NOT NULL AND payload->>'call_kind' = call_kind)
);
