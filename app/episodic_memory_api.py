"""Local read/curate history API. Tenant identity and publish sources are host-bound."""

from uuid import UUID

from fastapi import Header, HTTPException

from app.semantic_memory_api import create_semantic_memory_app
from domain.episodic_memory import BrowseEpisode, EpisodeQuery, EpisodeWithdrawal
from runtime.episode_ingest import EpisodeIngestor


def create_episodic_memory_app(episode_store, *, credentials, tasks=None):
    app = create_semantic_memory_app(episode_store.semantic, credentials=credentials, tasks=tasks)
    bindings = dict(tasks or {})

    def principal(value, write=False):
        import secrets

        if not value or not value.startswith("Bearer "):
            raise HTTPException(401, "memory_credential_required")
        owner = next(
            (p for token, p in credentials.items() if secrets.compare_digest(token.encode(), value[7:].encode())), None
        )
        if owner is None:
            raise HTTPException(401, "memory_credential_invalid")
        if write and owner.role != "curator":
            raise HTTPException(403, "memory_curator_required")
        return owner

    def run(function):
        from domain.execution import ExecutionRefused

        try:
            return function()
        except ExecutionRefused as exc:
            raise HTTPException(
                503
                if str(exc) == "execution_database_unavailable"
                else 404
                if str(exc) == "episode_not_found"
                else 409,
                str(exc),
            ) from None
        except (ValueError, OSError):
            raise HTTPException(409, "episode_scope_or_evidence_conflict") from None

    @app.post("/episodes/search")
    def search(query: EpisodeQuery, authorization: str | None = Header(default=None)):
        user = principal(authorization)
        return run(lambda: episode_store.search(user.user_id, query))

    @app.post("/episodes/publish/{task_id}")
    def publish(task_id: UUID, authorization: str | None = Header(default=None)):
        user = principal(authorization, True)
        binding = bindings.get(task_id)
        if binding is None or binding[1] != user.user_id:
            raise HTTPException(404, "memory_task_not_found")
        return run(lambda: EpisodeIngestor(episode_store).capture(binding[0], user.user_id, binding[2]))

    from pydantic import BaseModel, ConfigDict

    class PageRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        query: EpisodeQuery
        page: BrowseEpisode

    @app.post("/episodes/browse")
    def browse(request: PageRequest, authorization: str | None = Header(default=None)):
        user = principal(authorization)
        return run(lambda: episode_store.browse(user.user_id, request.query, request.page))

    @app.delete("/episodes/{episode_id}")
    def withdraw(episode_id: UUID, request: EpisodeWithdrawal, authorization: str | None = Header(default=None)):
        user = principal(authorization, True)
        return run(lambda: episode_store.withdraw(user.user_id, episode_id, request))

    @app.get("/episodes/usage/{task_id}")
    def usage(task_id: UUID, authorization: str | None = Header(default=None)):
        user = principal(authorization)
        return run(lambda: episode_store.usage(user.user_id, task_id))

    return app
