CREATE TABLE public.agent_episodes (
    episode_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    source_task_id uuid NOT NULL REFERENCES public.agent_execution_tasks(task_id),
    repo text NOT NULL,
    document jsonb NOT NULL,
    document_hash text NOT NULL,
    state text NOT NULL CHECK (state IN ('ACTIVE','REVOKED')),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(user_id,source_task_id)
);
CREATE TABLE public.agent_episode_blobs (
    episode_id uuid NOT NULL REFERENCES public.agent_episodes(episode_id),
    anchor_id uuid NOT NULL,
    text text NOT NULL,
    sha256 text NOT NULL,
    size_bytes integer NOT NULL CHECK (size_bytes<=65536),
    PRIMARY KEY(episode_id,anchor_id)
);
CREATE TABLE public.agent_episode_index (
    episode_id uuid PRIMARY KEY REFERENCES public.agent_episodes(episode_id),
    user_id uuid NOT NULL,
    repo text NOT NULL,
    document_hash text NOT NULL,
    terms text NOT NULL
);
CREATE TABLE public.agent_episode_dependencies (
    episode_id uuid NOT NULL REFERENCES public.agent_episodes(episode_id),
    parent_id uuid NOT NULL REFERENCES public.agent_semantic_memories(memory_id),
    parent_revision integer NOT NULL,
    parent_hash text NOT NULL,
    PRIMARY KEY(episode_id,parent_id)
);
CREATE TABLE public.agent_episode_parents (
    child_id uuid NOT NULL REFERENCES public.agent_episodes(episode_id),
    parent_id uuid NOT NULL REFERENCES public.agent_episodes(episode_id),
    parent_hash text NOT NULL,
    PRIMARY KEY(child_id,parent_id),
    CHECK(child_id<>parent_id)
);
CREATE TABLE public.agent_episode_usage (
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    sequence bigint NOT NULL,
    consumer_task_id uuid NOT NULL,
    episode_id uuid,
    request_id uuid,
    kind text NOT NULL CHECK (kind IN ('recalled','read','declared_adoption','rejected','execution_linked','late_link','analogy_verdict','withdrawn')),
    payload jsonb NOT NULL,
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(user_id,sequence),
    UNIQUE(user_id,request_id)
);
CREATE INDEX agent_episode_user_repo ON public.agent_episodes(user_id,repo,state);
