"""Pure lifecycle and pre-dispatch checks; not a persistent scheduler."""

from math import isfinite

from domain.tasks import Task, TaskLifecycle

ALLOWED_TRANSITIONS = {
    TaskLifecycle.CREATED: {
        TaskLifecycle.RUNNING,
        TaskLifecycle.WAITING_INPUT,
        TaskLifecycle.WAITING_APPROVAL,
        TaskLifecycle.CANCELLED,
    },
    TaskLifecycle.WAITING_INPUT: {TaskLifecycle.RUNNING, TaskLifecycle.WAITING_APPROVAL, TaskLifecycle.CANCELLED},
    TaskLifecycle.WAITING_APPROVAL: {TaskLifecycle.RUNNING, TaskLifecycle.WAITING_INPUT, TaskLifecycle.CANCELLED},
    TaskLifecycle.RUNNING: {
        TaskLifecycle.PAUSED,
        TaskLifecycle.WAITING_INPUT,
        TaskLifecycle.WAITING_APPROVAL,
        TaskLifecycle.COMPLETED,
        TaskLifecycle.FAILED,
        TaskLifecycle.CANCELLED,
    },
    TaskLifecycle.PAUSED: {
        TaskLifecycle.RUNNING,
        TaskLifecycle.WAITING_APPROVAL,
        TaskLifecycle.WAITING_INPUT,
        TaskLifecycle.CANCELLED,
    },
    TaskLifecycle.COMPLETED: set(),
    TaskLifecycle.FAILED: set(),
    TaskLifecycle.CANCELLED: set(),
}


class RunLimitExceeded(ValueError):
    pass


def transition_task(task: Task, target: TaskLifecycle) -> Task:
    if target not in ALLOWED_TRANSITIONS[task.lifecycle]:
        raise ValueError(f"illegal transition: {task.lifecycle} -> {target}")
    return Task.model_validate({**task.model_dump(), "lifecycle": target})


def _check_dispatch(task: Task, elapsed_seconds: float, *, readonly_nonrunning: bool = False) -> None:
    readonly_states = {TaskLifecycle.WAITING_INPUT, TaskLifecycle.WAITING_APPROVAL, TaskLifecycle.PAUSED}
    if task.lifecycle != TaskLifecycle.RUNNING and not (readonly_nonrunning and task.lifecycle in readonly_states):
        raise ValueError("calls require a RUNNING task")
    if not isfinite(elapsed_seconds) or elapsed_seconds < 0:
        raise ValueError("elapsed_seconds must be finite and nonnegative")
    if elapsed_seconds >= task.limits.max_wall_seconds:
        raise RunLimitExceeded("wall-clock limit reached before dispatch")


def reserve_tool_call(task: Task, *, elapsed_seconds: float, readonly_nonrunning: bool = False) -> Task:
    _check_dispatch(task, elapsed_seconds, readonly_nonrunning=readonly_nonrunning)
    if task.tool_calls_reserved >= task.limits.max_tool_calls:
        raise RunLimitExceeded("tool-call limit reached before dispatch")
    return Task.model_validate({**task.model_dump(), "tool_calls_reserved": task.tool_calls_reserved + 1})


def reserve_model_call(task: Task, *, elapsed_seconds: float, planning_nonrunning: bool = False) -> Task:
    _check_dispatch(task, elapsed_seconds, readonly_nonrunning=planning_nonrunning)
    if task.model_calls_reserved >= task.limits.max_model_calls:
        raise RunLimitExceeded("model-call limit reached before dispatch")
    return Task.model_validate({**task.model_dump(), "model_calls_reserved": task.model_calls_reserved + 1})
