"""Local product surface. Execution views and closed evaluation reports never share a context source."""

import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.planning_api import create_planning_app
from app.product_projection import memory_evidence
from app.semantic_memory_api import MemoryPrincipal, RevokeSource, create_semantic_memory_app
from domain.execution import ExecutionRefused
from domain.plans import ApprovalDecision, ControlRequest, document_hash
from domain.procedural_memory import SkillDecision
from domain.recovery import RecoveryDecision, RecoveryRequest
from runtime.task_state import RunLimitExceeded
from tools.readonly_workspace import WorkspaceRefused


@dataclass
class ProductHost:
    """Bindings are supplied by a trusted host, never by a browser or a model."""

    credentials: Mapping[str, str]
    loops: dict = field(default_factory=dict)
    archives: dict = field(default_factory=dict)
    reports: dict = field(default_factory=dict)
    recovery: Any = None
    semantic: Any = None
    skills: Any = None
    user_id: UUID | None = None
    demos: dict[str, Callable] = field(default_factory=dict)
    demo_lock: threading.Lock = field(default_factory=threading.Lock)

    def task_view(self, key):
        if key in self.archives:
            return self.archives[key]
        loop = self.loops.get(key)
        if loop is None:
            raise HTTPException(404, "registered_task_not_found")
        with loop.state_lock:
            view = loop.controller.view()
            snapshot = loop.session.files.snapshot()
            from domain.workspace_tools import DiffArgs

            patch = loop.session.files.execute(DiffArgs(), snapshot, 1048576)["patch"]
            calls, evidence = [], {}
            for path in loop.session.records.root.glob("calls/*/*.json"):
                record, request, result = loop.session.records.read(path)
                calls.append(record.model_dump(mode="json", exclude={"request_artifact", "result_artifact"}))
                if record.call_kind == "tool":
                    evidence[str(record.call_id)] = {
                        "request": request.model_dump(mode="json"),
                        "result": result.model_dump(mode="json"),
                        "output": loop.session.records.artifacts.read_text(result.artifact),
                    }
            hypothesis = loop.replanner.hypothesis
            current_hypothesis = (
                hypothesis is not None and hypothesis.subject_version == view["task"]["workspace_version"]
            )
            return {
                "id": key,
                "evidence_kind": "scripted_fixture" if key in self.demos else "live_execution",
                "read_only": False,
                "planning": view,
                "issue": loop.package.task.model_dump(mode="json"),
                "hypothesis": {
                    "latest": None if hypothesis is None else hypothesis.model_dump(mode="json"),
                    "version_state": "current_version" if current_hypothesis else "historical_or_absent",
                    "active": current_hypothesis and hypothesis.status in {"proposed", "supported", "inconclusive"},
                },
                "calls": sorted(calls, key=lambda c: c["recorded_at"]),
                "tool_evidence": evidence,
                "events": list(loop.events),
                "diff": patch,
                "tests": loop.tests,
                "public_complete": bool(loop.report and loop.report["completion_verified"]),
                "recovery": loop.recovery_info,
                "memory": memory_evidence(loop.session.records, loop.context_manager.settings),
            }


class QuiesceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str = Field(pattern="^(wait|stop)$")


class ApplyRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recovery_id: UUID


def create_product_app(host: ProductHost, *, static_root: Path | None = None):
    roles = {"viewer", "planner", "approver", "operator", "curator"}
    if set(host.credentials.values()) != roles or len(host.credentials) != len(roles):
        raise ValueError("five_distinct_product_roles_required")
    if any(len(token) < 32 for token in host.credentials):
        raise ValueError("long_product_credentials_required")
    app = FastAPI(title="Issue Repair Workbench", version="0.18.0")
    controllers = {UUID(k): v.controller for k, v in host.loops.items()}

    def require(value, allowed=roles):
        token = value[7:] if value and value.startswith("Bearer ") else ""
        role = next(
            (role for key, role in host.credentials.items() if secrets.compare_digest(key.encode(), token.encode())),
            None,
        )
        if role is None:
            raise HTTPException(401, "local_credential_required")
        if role not in allowed:
            raise HTTPException(403, "separate_product_role_required")
        return role

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, exc):
        return JSONResponse(
            status_code=422, content={"detail": [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]}
        )

    def invoke(action):
        try:
            return jsonable_encoder(action())
        except (ExecutionRefused, WorkspaceRefused, RunLimitExceeded, ValueError) as exc:
            # Existing controllers provide stable refusal codes; do not expose filesystem/DB errors.
            code = (
                str(exc) if isinstance(exc, (ExecutionRefused, WorkspaceRefused)) else "state_version_or_scope_conflict"
            )
            raise HTTPException(409, code) from None
        except OSError:
            raise HTTPException(503, "registered_evidence_unavailable") from None

    def loop(key):
        if key not in host.loops:
            raise HTTPException(409, "archive_is_read_only_or_task_not_registered")
        return host.loops[key]

    def recovery():
        if host.recovery is None:
            raise HTTPException(409, "recovery_host_not_configured")
        return host.recovery

    @app.get("/api/session")
    def session(authorization: str | None = Header(default=None)):
        return {"role": require(authorization)}

    @app.get("/api/tasks")
    def tasks(authorization: str | None = Header(default=None)):
        require(authorization)
        return [
            {"id": k, "issue": v["issue"], "read_only": v["read_only"], "evidence_kind": v["evidence_kind"]}
            for k in [*host.archives, *host.loops]
            for v in [host.task_view(k)]
        ]

    @app.get("/api/tasks/{key}")
    def task(key: str, authorization: str | None = Header(default=None)):
        require(authorization)
        return invoke(lambda: host.task_view(key))

    @app.get("/api/reports/{key}")
    def report(key: str, authorization: str | None = Header(default=None)):
        require(authorization, {"viewer"})
        if key in host.loops and not host.loops[key].closed:
            raise HTTPException(409, "evaluation_requires_closed_run")
        if key not in host.reports:
            raise HTTPException(404, "independent_evaluation_not_available")
        return host.reports[key]

    @app.post("/api/tasks/{key}/approval")
    def approve(key: str, request: ApprovalDecision, authorization: str | None = Header(default=None)):
        require(authorization, {"approver"})
        return invoke(lambda: loop(key).controller.decide(request))

    @app.post("/api/tasks/{key}/cancel")
    def cancel(key: str, request: ControlRequest, authorization: str | None = Header(default=None)):
        require(authorization, {"planner"})
        if request.action != "cancel":
            raise HTTPException(422, "cancel_action_required")
        return invoke(lambda: loop(key).controller.control(request))

    @app.post("/api/recovery/inspect")
    def inspect(request: RecoveryRequest, authorization: str | None = Header(default=None)):
        require(authorization, {"operator"})

        def run():
            review = recovery().inspect(request)
            return {
                **review.model_dump(mode="json"),
                "review_hash": document_hash(review),
                "plan_hash": document_hash(review.new_plan) if review.new_plan else None,
            }

        return invoke(run)

    @app.post("/api/recovery/{task_id}/quiesce")
    def quiesce(task_id: UUID, request: QuiesceRequest, authorization: str | None = Header(default=None)):
        require(authorization, {"operator"})
        return invoke(lambda: recovery().quiesce(task_id, action=request.action))

    @app.post("/api/recovery/decision")
    def decision(request: RecoveryDecision, authorization: str | None = Header(default=None)):
        require(authorization, {"approver"})
        return invoke(lambda: recovery().store.approve(request))

    @app.post("/api/recovery/apply")
    def apply(request: ApplyRecovery, authorization: str | None = Header(default=None)):
        require(authorization, {"operator"})

        def run():
            result = recovery().apply(request.recovery_id)
            if isinstance(result, dict):
                return result
            key = str(result.session.task.task_id)
            host.archives.pop(key, None)
            host.loops[key] = result
            controllers[UUID(key)] = result.controller
            return {
                "task_id": key,
                "restored": True,
                "new_authorization": result.controller.view()["bound_authorization_id"],
            }

        return invoke(run)

    @app.delete("/api/memory/sources/{source_id}")
    def revoke(source_id: UUID, request: RevokeSource, authorization: str | None = Header(default=None)):
        require(authorization, {"curator"})
        if host.semantic is None or host.user_id is None:
            raise HTTPException(409, "memory_host_not_configured")
        return invoke(lambda: host.semantic.revoke_source(host.user_id, source_id, request.request_id, request.reason))

    @app.post("/api/skills/decision")
    def skill(request: SkillDecision, authorization: str | None = Header(default=None)):
        require(authorization, {"curator"})
        if host.skills is None or host.user_id is None:
            raise HTTPException(409, "skill_host_not_configured")
        return invoke(lambda: host.skills.decide(host.user_id, request))

    @app.post("/api/demos/{key}/advance")
    def advance(key: str, authorization: str | None = Header(default=None)):
        require(authorization, {"planner"})
        if key not in host.demos:
            raise HTTPException(404, "registered_scripted_demo_not_found")
        if not host.demo_lock.acquire(blocking=False):
            raise HTTPException(409, "demo_already_running")
        try:
            return invoke(host.demos[key])
        finally:
            host.demo_lock.release()

    # Preserve the existing full planning API, including proposals, hints and tool access.
    token_for = {v: k for k, v in host.credentials.items()}
    app.mount(
        "/planning",
        create_planning_app(controllers, planner_token=token_for["planner"], approver_token=token_for["approver"]),
    )
    if host.semantic is not None and host.user_id is not None:
        app.mount(
            "/memory",
            create_semantic_memory_app(
                host.semantic,
                credentials={
                    token_for[r]: MemoryPrincipal(user_id=host.user_id, role="curator" if r == "curator" else "reader")
                    for r in ("viewer", "curator")
                },
            ),
        )
    static_root = static_root or Path(__file__).parent / "web/dist"
    if static_root.is_dir():
        app.mount("/", StaticFiles(directory=static_root, html=True), name="workbench")
    return app
