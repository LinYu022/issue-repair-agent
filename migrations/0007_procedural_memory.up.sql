CREATE TABLE public.agent_skills (
    skill_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    name text NOT NULL,
    latest_revision integer NOT NULL,
    enabled_revision integer,
    UNIQUE(user_id,name)
);
CREATE TABLE public.agent_skill_revisions (
    skill_id uuid NOT NULL REFERENCES public.agent_skills(skill_id),
    revision integer NOT NULL,
    document jsonb NOT NULL,
    document_hash text NOT NULL,
    state text NOT NULL CHECK(state IN ('CANDIDATE','VALIDATED','ENABLED','DEPRECATED','REVOKED')),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    validated_at timestamptz,
    enabled_at timestamptz,
    PRIMARY KEY(skill_id,revision)
);
CREATE TABLE public.agent_skill_sources (
    skill_id uuid NOT NULL,
    revision integer NOT NULL,
    episode_id uuid NOT NULL REFERENCES public.agent_episodes(episode_id),
    document_hash text NOT NULL,
    PRIMARY KEY(skill_id,revision,episode_id),
    FOREIGN KEY(skill_id,revision) REFERENCES public.agent_skill_revisions(skill_id,revision)
);
CREATE TABLE public.agent_skill_validation_tasks (
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    task_id uuid NOT NULL REFERENCES public.agent_execution_tasks(task_id),
    declaration jsonb NOT NULL,
    declaration_hash text NOT NULL,
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(user_id,task_id)
);
CREATE TABLE public.agent_skill_attempts (
    attempt_id uuid PRIMARY KEY,
    user_id uuid NOT NULL,
    skill_id uuid NOT NULL,
    revision integer NOT NULL,
    task_id uuid NOT NULL REFERENCES public.agent_execution_tasks(task_id),
    mode text NOT NULL CHECK(mode IN ('validation','use')),
    proposal jsonb NOT NULL,
    proposal_hash text NOT NULL,
    evidence jsonb,
    evidence_hash text,
    state text NOT NULL CHECK(state IN ('PROPOSED','PASSED','FAILED','REJECTED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    FOREIGN KEY(skill_id,revision) REFERENCES public.agent_skill_revisions(skill_id,revision),
    UNIQUE(user_id,skill_id,revision,task_id)
);
CREATE TABLE public.agent_skill_events (
    user_id uuid NOT NULL REFERENCES public.agent_memory_namespaces(user_id),
    sequence bigint NOT NULL,
    request_id uuid,
    payload jsonb NOT NULL,
    payload_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(user_id,sequence),
    UNIQUE(user_id,request_id)
);
