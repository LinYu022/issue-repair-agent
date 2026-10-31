"""Local authenticated planning API with distinct planner and approver credentials.

Tokens are supplied by the trusted host at startup, never accepted in plan data.
An approver token must not be given to the model or mounted into a task container.
This local mechanism is not a multi-user login, external identity provider or TLS.
"""

import secrets
from collections.abc import Mapping
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from domain.models import ToolObservation
from domain.plans import ApprovalDecision, ControlRequest, HumanHint, PlanProposal, RevokeAuthorization
from domain.workspace_tools import WorkspaceToolRequest
from runtime.plan_control import PlanController
from runtime.task_state import RunLimitExceeded
from tools.readonly_workspace import WorkspaceRefused


def create_planning_app(controllers: Mapping[UUID, PlanController], *, planner_token: str, approver_token: str):
    if min(len(planner_token), len(approver_token)) < 32 or secrets.compare_digest(planner_token, approver_token):
        raise ValueError("distinct_long_planner_and_approver_credentials_required")
    app = FastAPI(title="Issue Agent local Plan/Execute approval controller", version="0.5.0")

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422, content={"detail": [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]}
        )

    def role(authorization):
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="local_credential_required")
        candidate = authorization[7:]
        if secrets.compare_digest(candidate.encode(), planner_token.encode()):
            return "planner"
        if secrets.compare_digest(candidate.encode(), approver_token.encode()):
            return "approver"
        raise HTTPException(status_code=401, detail="local_credential_invalid")

    def require(authorization, expected):
        if role(authorization) != expected:
            raise HTTPException(status_code=403, detail="separate_approver_or_planner_role_required")

    def controller(task_id):
        current = controllers.get(task_id)
        if current is None or current.session.task.task_id != task_id:
            raise HTTPException(status_code=404, detail="registered_planning_task_not_found")
        return current

    def invoke(action):
        try:
            return action()
        except (WorkspaceRefused, RunLimitExceeded) as exc:
            raise HTTPException(status_code=409, detail={"code": str(exc)}) from None
        except ValueError:
            raise HTTPException(status_code=409, detail="planning_scope_state_or_revision_conflict") from None
        except OSError:
            raise HTTPException(status_code=503, detail="planning_evidence_unavailable") from None

    @app.get("/tasks/{task_id}/planning")
    def view(task_id: UUID, authorization: str | None = Header(default=None)):
        role(authorization)
        return controller(task_id).view()

    @app.post("/tasks/{task_id}/plans")
    def propose(task_id: UUID, request: PlanProposal, authorization: str | None = Header(default=None)):
        require(authorization, "planner")
        return invoke(lambda: controller(task_id).propose(request))

    @app.post("/tasks/{task_id}/approval")
    def approve(task_id: UUID, request: ApprovalDecision, authorization: str | None = Header(default=None)):
        require(authorization, "approver")
        return invoke(lambda: controller(task_id).decide(request))

    @app.post("/tasks/{task_id}/revoke")
    def revoke(task_id: UUID, request: RevokeAuthorization, authorization: str | None = Header(default=None)):
        require(authorization, "approver")
        return invoke(lambda: controller(task_id).revoke(request))

    @app.post("/tasks/{task_id}/human-hints")
    def hint(task_id: UUID, request: HumanHint, authorization: str | None = Header(default=None)):
        require(authorization, "approver")
        return invoke(lambda: controller(task_id).hint(request))

    @app.post("/tasks/{task_id}/control")
    def control(task_id: UUID, request: ControlRequest, authorization: str | None = Header(default=None)):
        require(authorization, "planner")
        return invoke(lambda: controller(task_id).control(request))

    @app.post("/tasks/{task_id}/tools", response_model=ToolObservation)
    def tool(task_id: UUID, request: WorkspaceToolRequest, authorization: str | None = Header(default=None)):
        require(authorization, "planner")
        if request.task_id != task_id:
            raise HTTPException(status_code=409, detail="request_task_mismatch")
        return invoke(lambda: controller(task_id).session.call(request))

    return app
