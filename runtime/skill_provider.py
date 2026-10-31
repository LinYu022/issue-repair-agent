"""Host-bound workflow recall and selected plan drafts; never invokes workspace tools."""

import json
from uuid import UUID

from domain.procedural_memory import SkillRef
from runtime.episode_provider import EpisodicMemoryProvider, attach_episodic_memory
from runtime.semantic_rules import applies
from tools.readonly_workspace import WorkspaceRefused


class ProceduralMemoryProvider(EpisodicMemoryProvider):
    def __init__(self, loop, semantic, episodes, skills, user_id, branch):
        super().__init__(loop, semantic, episodes, user_id, branch)
        self.skills = skills
        self.skill_selection = None
        self.skill_cursor = 0
        self.skill_failed = False

    def binding(self):
        return {**super().binding(), "procedural": True}

    def retrieve(self, *, task_id, workspace_version):
        items = super().retrieve(task_id=task_id, workspace_version=workspace_version)
        skills = self.skills.search(self.user_id, self.query(text=self.loop.session.task.issue[:1000], limit=2))
        return (
            *items,
            *(
                {
                    "memory_type": "procedure",
                    "workflow": value,
                    "grants_authority": False,
                    "instruction": "Workflow advice only. Propose a current plan and obtain current authorization; never execute a historical grant or fixed patch.",
                }
                for value in skills
            ),
        )

    def select(self, ref, parameters, *, mode="use"):
        self.loop.session.durable.check()
        proposal = self.skills.propose(
            self.user_id, ref, self.query(), parameters, self.loop.session.profiles, mode=mode
        )
        self.skill_selection = proposal
        self.skill_cursor, self.skill_failed = 0, False
        # This is a read/validation workflow, with its own ordered first read.
        # Repair workflows without a selected Skill retain their baseline preflight.
        self.loop.preflight_done = True
        return proposal

    def before_tool(self, arguments):
        if not self.skill_selection:
            return
        self.context_expansion()
        actions = self.skill_selection["actions"]
        normalized = {key: arguments[key] for key in ("operation", "path", "profile_id") if key in arguments}
        if (
            self.skill_failed
            or self.skill_cursor >= len(actions)
            or normalized != actions[self.skill_cursor]
            or arguments.get("start_line") is not None
        ):
            with self.skills.semantic.transaction(self.user_id) as (c, _):
                self.skills._event(
                    c,
                    self.user_id,
                    "workflow_action_refused",
                    {
                        "attempt_id": self.skill_selection["attempt_id"],
                        "task_id": str(self.loop.session.task.task_id),
                        "reason": "failed_or_out_of_order_or_disallowed_action",
                    },
                )
            raise WorkspaceRefused("skill_stop_or_exact_next_action_required")

    def after_tool(self, request, observation):
        if not self.skill_selection:
            return
        record, _, _ = self.loop.session.records.read(
            self.loop.session.records.root / "calls/tool" / f"{request.call_id}.json"
        )
        from domain.plans import document_hash

        self.skill_failed = observation.status.value != "SUCCEEDED"
        self.skill_cursor += 1
        with self.skills.semantic.transaction(self.user_id) as (c, _):
            self.skills._event(
                c,
                self.user_id,
                "execution_linked",
                {
                    "attempt_id": self.skill_selection["attempt_id"],
                    "task_id": str(request.task_id),
                    "call_id": str(request.call_id),
                    "record_hash": document_hash(record),
                    "status": observation.status.value,
                    "benefit_proven": False,
                },
            )

    def context_expansion(self):
        values = list(super().context_expansion())
        if self.skill_selection:
            ref = SkillRef.model_validate(self.skill_selection["skill_ref"])
            # Revalidate every build. Validation-only drafts are host test inputs and
            # must not silently become generally enabled workflows.
            with self.skills.semantic.transaction(self.user_id) as (c, _):
                row, doc = self.skills._entry(c, self.user_id, ref)
                if row["state"] == "REVOKED" or not self.skills._sources_live(c, doc):
                    raise WorkspaceRefused("skill_selected_version_withdrawn")
                if not applies(doc, self.query()):
                    raise WorkspaceRefused("skill_selected_scope_changed")
                if row["state"] != "ENABLED" or row["enabled_revision"] != ref.revision:
                    attempt = c.execute(
                        "SELECT mode,state FROM public.agent_skill_attempts WHERE user_id=%s AND attempt_id=%s",
                        (self.user_id, UUID(self.skill_selection["attempt_id"])),
                    ).fetchone()
                    if (
                        not attempt
                        or attempt["mode"] != "validation"
                        or attempt["state"] != "PROPOSED"
                        or row["state"] not in {"CANDIDATE", "VALIDATED"}
                    ):
                        raise WorkspaceRefused("skill_selected_version_no_longer_enabled")
            from runtime.skill_rules import bind

            # Scope, watched files, parameter values, and current version are fresh.
            current = bind(doc, self.query(), self.skill_selection["parameters"], self.loop.session.profiles)
            if current["workspace_version"] != self.skill_selection["workspace_version"]:
                raise WorkspaceRefused("skill_selected_workspace_changed_reselect_required")
            values.append({"selected_workflow_plan_draft": self.skill_selection, "grants_authority": False})
        return values


def attach_procedural_memory(loop, semantic, episodes, skills, *, user_id, branch):
    current = loop.context_manager.memory
    if not isinstance(current, EpisodicMemoryProvider):
        attach_episodic_memory(loop, semantic, episodes, user_id=user_id, branch=branch)
        current = loop.context_manager.memory
    if isinstance(current, ProceduralMemoryProvider) or (current.user_id, current.branch) != (user_id, branch):
        raise WorkspaceRefused("skill_host_identity_or_duplicate_binding")
    provider = ProceduralMemoryProvider(loop, semantic, episodes, skills, user_id, branch)
    loop.context_manager.memory = provider
    loop.messages[0]["content"] += (
        '\nProcedural memory is enabled. Workflow recall provides versioned plan templates. Select one with {"operation":"plan_skill","target":{"skill_id":"UUID","revision":1,"document_hash":"SHA256"},"parameters":{"source":"path","test_0":"registered-profile"}}. This only proposes a plan; wait for current approval, then use the existing tools. Preconditions and failures are mandatory; no Skill action can authorize itself.\n'
    )
    (loop.context_manager.root / "memory-binding.json").write_text(json.dumps(provider.binding(), indent=2) + "\n")
    return provider
