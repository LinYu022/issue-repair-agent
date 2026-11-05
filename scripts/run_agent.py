"""Run a local Python issue through the existing repair loop using a real DeepSeek model."""

import argparse
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from adapters.deepseek import DeepSeekClient, ModelServiceError
from adapters.models import AdapterOutcomeUnknown
from domain.context import ContextSettings
from domain.model_calls import ModelReply
from domain.plans import ApprovalDecision, PlanScope
from domain.repair_loop import RepairRegistration
from domain.replanning import ReplanSettings
from domain.tasks import RunLimits
from domain.workspace_tools import OPERATIONS, TaskPackage, TestProfile, VisibleFile, VisibleTask
from domain.workspaces import relative_parts
from runtime.repair_setup import make_repair_loop
from tools.readonly_workspace import require_linux_capabilities
from tools.workspace_files import sha


class ModelAdapter:
    evidence_kind = "real_execution"

    def __init__(self, client):
        self.client, self.model_name = client, client.model

    def generate(self, request):
        try:
            text = self.client.query(json.loads(request.prompt))
        except ModelServiceError:
            raise AdapterOutcomeUnknown("model_response_not_confirmed") from None
        return ModelReply(task_id=request.task_id, request_id=request.request_id, model_name=self.model_name, text=text)


def load_task(path, max_calls):
    value = json.loads(path.read_text(encoding="utf-8"))
    root = path.parent.resolve()
    files = []
    for name in value["files"]:
        relative_parts(name)
        source = root / name
        if not source.resolve().is_relative_to(root):
            raise ValueError("task_source_outside_config_directory")
        text = source.read_text(encoding="utf-8")
        files.append(VisibleFile(path=name, text=text, sha256=sha(text.encode())))
    package = TaskPackage(
        task=VisibleTask(
            task_id="local-" + uuid4().hex,
            source_kind="contract_fixture" if value["repo"].startswith("fixture/") else "self_built",
            repo=value["repo"],
            base_commit=value.get("base_commit"),
            issue=value["issue"],
            environment_id="python312-local",
            available_at=datetime.now(timezone.utc),
        ),
        files=tuple(files),
    )
    profiles = {name: TestProfile(profile_id=name, argv=tuple(argv)) for name, argv in value["test_profiles"].items()}
    scope = PlanScope(
        read_paths=(".",),
        write_paths=tuple(value["write_paths"]),
        operations=OPERATIONS,
        test_profiles=tuple(profiles),
        limits=RunLimits(max_model_calls=max_calls, max_tool_calls=40, max_wall_seconds=600),
        max_test_seconds=30,
    )
    registration = RepairRegistration(
        scope=scope,
        reproduction_profile=value["reproduction_profile"],
        required_profiles=tuple(profiles),
        protected_paths=tuple(value["protected_paths"]),
        replan_policy=True,
    )
    return package, registration, profiles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--image", default="issue-agent-runner:latest")
    parser.add_argument("--model", default=os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    parser.add_argument("--base-url", default=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--max-calls", type=int, default=12)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--durable", action="store_true", help="Use the configured PostgreSQL execution ledger and checkpoints"
    )
    parser.add_argument("--memory", choices=["none", "semantic", "episodic", "all"], default="none")
    parser.add_argument("--user-id", type=UUID)
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    require_linux_capabilities()
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        parser.error("set DEEPSEEK_API_KEY in the host environment")
    if args.memory != "none" and (not args.durable or args.user_id is None):
        parser.error("memory requires --durable and --user-id")
    client = DeepSeekClient(
        key, model=args.model, base_url=args.base_url, max_calls=args.max_calls, stop=["<｜DSML｜function_calls>"]
    )
    package, registration, profiles = load_task(args.task.resolve(), args.max_calls)
    inspected = subprocess.run(
        ["docker", "image", "inspect", args.image, "--format", "{{.Id}}"], capture_output=True, text=True, check=True
    )
    image = inspected.stdout.strip()
    output = (args.output or Path.home() / ".local/share/issue-repair-agent/runs" / uuid4().hex).resolve()
    output.mkdir(parents=True, mode=0o700)
    workspaces = output / "workspaces"
    workspaces.mkdir()
    execution = None
    if args.durable:
        from runtime.execution_store import PostgresExecutionStore
        from runtime.postgres import database_url

        execution = PostgresExecutionStore(database_url())
    loop = make_repair_loop(
        workspaces,
        output / "trace",
        package,
        registration,
        profiles,
        image,
        provenance="local_operator",
        replan_settings=ReplanSettings(enabled=True),
        context_settings=ContextSettings(mode="structured"),
        durable_store=execution,
        lock_root=output / "locks",
    )
    try:
        if args.memory != "none":
            from runtime.episode_provider import attach_episodic_memory
            from runtime.episode_store import EpisodeStore
            from runtime.semantic_provider import attach_semantic_memory
            from runtime.semantic_store import SemanticStore
            from runtime.skill_provider import attach_procedural_memory
            from runtime.skill_store import SkillStore

            semantic = SemanticStore(execution)
            if args.memory == "semantic":
                attach_semantic_memory(loop, semantic, user_id=args.user_id, branch=args.branch)
            elif args.memory == "episodic":
                attach_episodic_memory(
                    loop, semantic, EpisodeStore(execution), user_id=args.user_id, branch=args.branch
                )
            else:
                attach_procedural_memory(
                    loop,
                    semantic,
                    EpisodeStore(execution),
                    SkillStore(execution),
                    user_id=args.user_id,
                    branch=args.branch,
                )
        if args.durable:
            from runtime.recovery import register_recovery

            recovery = output / "recovery"
            recovery.mkdir()
            register_recovery(loop, recovery)
        print("运行目录：" + str(output), flush=True)
        adapter = ModelAdapter(client)
        while not loop.closed:
            result = loop.run(adapter)
            if not result or not result.get("waiting_approval"):
                break
            view = loop.controller.view()
            print(json.dumps(view["plan"], ensure_ascii=False, indent=2), flush=True)
            decision = input("确认上述计划与修改范围后，输入 approve 批准；其他输入拒绝：").strip()
            loop.controller.decide(
                ApprovalDecision(
                    revision_id=view["plan"]["revision_id"],
                    revision_hash=view["revision_hash"],
                    expected_workspace_version=view["task"]["workspace_version"],
                    decision="approve" if decision == "approve" else "deny",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                    reason="local operator reviewed the displayed plan",
                )
            )
            if decision != "approve":
                loop.seal("operator_denied", "wait_input")
            elif args.durable:
                from runtime.checkpoints import JointCheckpoints

                JointCheckpoints(loop).create()
        if not loop.closed:
            loop.seal("host_stopped", "pause")
        if args.durable:
            from runtime.checkpoints import JointCheckpoints

            JointCheckpoints(loop).create()
        from fastapi.encoders import jsonable_encoder

        from app.product_api import ProductHost

        task_id = str(loop.session.task.task_id)
        snapshot = ProductHost(credentials={}, loops={task_id: loop}).task_view(task_id)
        snapshot.update(read_only=True, evidence_kind="real_model_history")
        (output / "workbench.json").write_text(
            json.dumps(
                jsonable_encoder({"archives": {task_id: snapshot}, "reports": {}}), ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "reason": loop.report["reason"],
                    "public_complete": loop.report["completion_verified"],
                    "patch": str(loop.root / "submission.patch"),
                    "model_calls": client.calls,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 0 if loop.report["completion_verified"] else 1
    finally:
        if not loop.closed:
            loop.seal("host_interrupted", "pause")
        if loop.session.durable:
            loop.session.durable.close()


if __name__ == "__main__":
    raise SystemExit(main())
