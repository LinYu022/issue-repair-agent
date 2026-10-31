CREATE TABLE public.agent_memory_namespaces (
    user_id uuid PRIMARY KEY,
    epoch bigint NOT NULL DEFAULT 0
);
CREATE TABLE public.agent_memory_sources (
    source_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    repo text NOT NULL,
    payload jsonb NOT NULL,
    payload_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('LIVE','INVALIDATED','REVOKED')),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.agent_memory_jobs (
    job_id uuid PRIMARY KEY,
    source_id uuid NOT NULL UNIQUE REFERENCES public.agent_memory_sources(source_id),
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    state text NOT NULL CHECK (state IN ('PENDING','RUNNING','DONE','CANCELLED','FAILED')),
    worker_id uuid,
    lease_epoch bigint NOT NULL DEFAULT 0,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    memory_id uuid,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.agent_semantic_memories (
    memory_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    repo text NOT NULL,
    state text NOT NULL CHECK (state IN ('CANDIDATE','ACTIVE','STALE','CONFLICT','REVOKED','SUPERSEDED')),
    current_revision integer NOT NULL,
    current_hash text NOT NULL,
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE public.agent_memory_revisions (
    memory_id uuid NOT NULL REFERENCES public.agent_semantic_memories(memory_id),
    revision integer NOT NULL,
    document jsonb NOT NULL,
    document_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(memory_id,revision)
);
CREATE TABLE public.agent_memory_dependencies (
    parent_id uuid NOT NULL REFERENCES public.agent_semantic_memories(memory_id),
    child_id uuid NOT NULL REFERENCES public.agent_semantic_memories(memory_id),
    parent_revision integer NOT NULL,
    parent_hash text NOT NULL,
    PRIMARY KEY(parent_id,child_id),
    CHECK(parent_id <> child_id)
);
CREATE TABLE public.agent_memory_active_index (
    memory_id uuid PRIMARY KEY REFERENCES public.agent_semantic_memories(memory_id),
    user_id uuid NOT NULL,
    repo text NOT NULL,
    revision integer NOT NULL,
    document_hash text NOT NULL,
    terms text NOT NULL
);
CREATE TABLE public.agent_memory_cache (
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    query_hash text NOT NULL,
    epoch bigint NOT NULL,
    items jsonb NOT NULL,
    PRIMARY KEY(user_id,query_hash)
);
CREATE TABLE public.agent_memory_events (
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    sequence bigint NOT NULL,
    request_id uuid,
    payload jsonb NOT NULL,
    payload_hash text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(user_id,sequence),
    UNIQUE(user_id,request_id)
);
CREATE INDEX agent_memory_lookup ON public.agent_semantic_memories(user_id,repo,state);
