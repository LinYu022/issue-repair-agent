"""Local memory-owner API. Trusted host credentials bind users/roles; models cannot promote notes."""

import secrets
from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from domain.execution import ExecutionRefused
from domain.semantic_memory import EnvironmentChange, MemoryAmend, MemoryQuery, MemoryReview, UserConfirmation
from runtime.semantic_ingest import SemanticIngestor


class MemoryPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    user_id: UUID
    role: Literal["reader", "curator"]


class SourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    call_id: UUID
    key: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")
    quote: str = Field(default="", max_length=2400)


class RevokeSource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request_id: UUID
    reason: str = Field(min_length=1, max_length=1000)


def create_semantic_memory_app(store, *, credentials: Mapping[str, MemoryPrincipal], tasks: Mapping | None = None):
    if not credentials or any(len(token) < 32 for token in credentials):
        raise ValueError("long_distinct_memory_credentials_required")
    credentials = dict(credentials)
    tasks = tasks or {}
    ingest = SemanticIngestor(store)
    app = FastAPI(title="Issue Agent local semantic memory", version="0.13.0")

    @app.exception_handler(RequestValidationError)
    async def bad_input(_request, exc):
        return JSONResponse(
            status_code=422, content={"detail": [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]}
        )

    def principal(authorization, *, write=False):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="memory_credential_required")
        found = next(
            (
                p
                for token, p in credentials.items()
                if secrets.compare_digest(token.encode(), authorization[7:].encode())
            ),
            None,
        )
        if found is None:
            raise HTTPException(status_code=401, detail="memory_credential_invalid")
        if write and found.role != "curator":
            raise HTTPException(status_code=403, detail="memory_curator_required")
        return found

    def invoke(function):
        try:
            return function()
        except ExecutionRefused as exc:
            code = str(exc)
            if code == "execution_database_unavailable":
                raise HTTPException(status_code=503, detail="memory_database_unavailable") from None
            raise HTTPException(status_code=404 if code.endswith("not_found") else 409, detail=code) from None
        except (ValueError, OSError):
            raise HTTPException(status_code=409, detail="memory_scope_state_or_evidence_conflict") from None

    @app.post("/sources/preferences")
    def preference(request: UserConfirmation, authorization: str | None = Header(default=None)):
        owner = principal(authorization, write=True)
        return invoke(
            lambda: {
                "source_id": str(ingest.confirmation(owner.user_id, request)),
                "job_id": str(store.enqueue(owner.user_id, request.source_id)),
            }
        )

    @app.post("/tasks/{task_id}/sources/{origin}")
    def visible_source(
        task_id: UUID,
        origin: Literal["document", "execution"],
        request: SourceRequest,
        authorization: str | None = Header(default=None),
    ):
        owner = principal(authorization, write=True)
        binding = tasks.get(task_id)
        if binding is None or binding[1] != owner.user_id:
            raise HTTPException(status_code=404, detail="memory_task_not_found")
        loop, _, branch = binding

        def operation():
            source_id = (
                ingest.document(loop, owner.user_id, branch, request.call_id, request.key, request.quote)
                if origin == "document"
                else ingest.execution(loop, owner.user_id, branch, request.call_id, request.key)
            )
            return {"source_id": str(source_id), "job_id": str(store.enqueue(owner.user_id, source_id))}

        return invoke(operation)

    @app.get("/memories/candidates")
    def candidates(authorization: str | None = Header(default=None)):
        owner = principal(authorization)
        return invoke(lambda: store.candidates(owner.user_id))

    @app.get("/memories/{memory_id}")
    def read(memory_id: UUID, authorization: str | None = Header(default=None)):
        owner = principal(authorization)
        return invoke(lambda: store.read(owner.user_id, memory_id))

    @app.post("/memories/review")
    def review(request: MemoryReview, authorization: str | None = Header(default=None)):
        owner = principal(authorization, write=True)
        return invoke(lambda: store.review(owner.user_id, request))

    @app.post("/memories/{memory_id}/amend")
    def amend(memory_id: UUID, request: MemoryAmend, authorization: str | None = Header(default=None)):
        owner = principal(authorization, write=True)
        return invoke(lambda: store.amend(owner.user_id, memory_id, request))

    @app.delete("/sources/{source_id}")
    def delete(source_id: UUID, request: RevokeSource, authorization: str | None = Header(default=None)):
        owner = principal(authorization, write=True)
        return invoke(lambda: store.revoke_source(owner.user_id, source_id, request.request_id, request.reason))

    @app.get("/sources/{source_id}")
    def source(source_id: UUID, authorization: str | None = Header(default=None)):
        owner = principal(authorization)
        return invoke(lambda: store.read_source(owner.user_id, source_id))

    @app.post("/memories/invalidate")
    def invalidate(request: EnvironmentChange, authorization: str | None = Header(default=None)):
        owner = principal(authorization, write=True)
        return invoke(lambda: store.invalidate(owner.user_id, request))

    @app.post("/memories/query")
    def query(request: MemoryQuery, authorization: str | None = Header(default=None)):
        owner = principal(authorization)
        return invoke(lambda: store.retrieve(owner.user_id, request))

    return app
