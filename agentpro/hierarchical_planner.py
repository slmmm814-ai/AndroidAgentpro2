from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from .models import ActionType, AgentAction, AgentContext


class HierarchyValidationError(ValueError):
    """Raised when a hierarchical task is structurally invalid."""


class HierarchicalPlanningError(RuntimeError):
    """Raised when a hierarchical plan cannot produce executable actions."""


@dataclass(frozen=True)
class TaskStep:
    name: str
    action: AgentAction
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise HierarchyValidationError("step name must not be empty")
        if not isinstance(self.action, AgentAction):
            raise HierarchyValidationError("step action must be an AgentAction")
        if not isinstance(self.metadata, Mapping):
            raise HierarchyValidationError("step metadata must be a mapping")


@dataclass(frozen=True)
class SubTask:
    name: str
    steps: tuple[TaskStep, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise HierarchyValidationError("subtask name must not be empty")
        if not isinstance(self.steps, tuple):
            raise HierarchyValidationError("subtask steps must be a tuple")
        if not self.steps:
            raise HierarchyValidationError("subtask must contain at least one step")
        if not isinstance(self.metadata, Mapping):
            raise HierarchyValidationError("subtask metadata must be a mapping")
        for step in self.steps:
            if not isinstance(step, TaskStep):
                raise HierarchyValidationError(
                    "all subtask steps must be TaskStep instances"
                )


@dataclass(frozen=True)
class HierarchicalTask:
    name: str
    subtasks: tuple[SubTask, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise HierarchyValidationError("task name must not be empty")
        if not isinstance(self.subtasks, tuple):
            raise HierarchyValidationError("task subtasks must be a tuple")
        if not self.subtasks:
            raise HierarchyValidationError("task must contain at least one subtask")
        if not isinstance(self.metadata, Mapping):
            raise HierarchyValidationError("task metadata must be a mapping")
        for subtask in self.subtasks:
            if not isinstance(subtask, SubTask):
                raise HierarchyValidationError(
                    "all task subtasks must be SubTask instances"
                )

    @property
    def depth(self) -> int:
        return 3

    @property
    def action_count(self) -> int:
        return sum(len(subtask.steps) for subtask in self.subtasks)

    def flatten(self) -> list[AgentAction]:
        actions: list[AgentAction] = []
        for subtask in self.subtasks:
            for step in subtask.steps:
                actions.append(step.action)
        return actions


class HierarchicalTaskProvider(Protocol):
    def build(self, context: AgentContext) -> HierarchicalTask:
        ...


class StaticHierarchicalTaskProvider:
    """Deterministic hierarchical task provider for execution and tests."""

    def __init__(self, task: HierarchicalTask) -> None:
        if not isinstance(task, HierarchicalTask):
            raise TypeError("task must be a HierarchicalTask")
        self._task = task

    def build(self, context: AgentContext) -> HierarchicalTask:
        context.validate()
        return self._task


class HierarchicalPlanner:
    """
    Deterministic three-level planner.

    Level 1 is the HierarchicalTask.
    Level 2 consists of SubTask objects.
    Level 3 consists of TaskStep objects.

    The planner validates the hierarchy and exposes the executable actions
    through the same plan(context) contract used by AgentExecutor.
    """

    def __init__(
        self,
        provider: HierarchicalTaskProvider,
        *,
        max_actions: int = 50,
    ) -> None:
        if not hasattr(provider, "build"):
            raise TypeError("provider must implement build()")
        if isinstance(max_actions, bool) or not isinstance(max_actions, int):
            raise TypeError("max_actions must be an integer")
        if max_actions < 1:
            raise ValueError("max_actions must be >= 1")

        self.provider = provider
        self.max_actions = max_actions
        self.last_task: HierarchicalTask | None = None

    def build(self, context: AgentContext) -> HierarchicalTask:
        context.validate()

        task = self.provider.build(context)
        if not isinstance(task, HierarchicalTask):
            raise HierarchicalPlanningError(
                "hierarchical provider must return HierarchicalTask"
            )

        if task.depth != 3:
            raise HierarchyValidationError(
                f"hierarchical task must have depth 3, got {task.depth}"
            )

        action_count = task.action_count
        remaining = self.max_actions - context.action_count
        if action_count > remaining:
            raise HierarchicalPlanningError(
                f"hierarchical task contains {action_count} actions but "
                f"only {remaining} actions remain"
            )

        actions = task.flatten()
        if len(actions) != action_count:
            raise HierarchicalPlanningError(
                "hierarchical action count does not match flattened actions"
            )

        self.last_task = task
        return task

    def plan(self, context: AgentContext) -> list[AgentAction]:
        task = self.build(context)
        return task.flatten()

    def describe(self) -> dict[str, Any]:
        if self.last_task is None:
            return {
                "depth": 0,
                "task": None,
                "subtasks": [],
                "action_count": 0,
            }

        return {
            "depth": self.last_task.depth,
            "task": self.last_task.name,
            "subtasks": [
                {
                    "name": subtask.name,
                    "steps": [
                        {
                            "name": step.name,
                            "action": step.action.action_type.value,
                            "arguments": dict(step.action.arguments),
                            "requires_confirmation": step.action.requires_confirmation,
                        }
                        for step in subtask.steps
                    ],
                }
                for subtask in self.last_task.subtasks
            ],
            "action_count": self.last_task.action_count,
        }


def make_step(
    name: str,
    action_type: ActionType,
    arguments: Mapping[str, Any] | None = None,
    *,
    requires_confirmation: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> TaskStep:
    return TaskStep(
        name=name,
        action=AgentAction(
            action_type=action_type,
            arguments=dict(arguments or {}),
            requires_confirmation=requires_confirmation,
        ),
        metadata=dict(metadata or {}),
    )


def make_subtask(
    name: str,
    steps: Sequence[TaskStep],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> SubTask:
    return SubTask(
        name=name,
        steps=tuple(steps),
        metadata=dict(metadata or {}),
    )


def make_task(
    name: str,
    subtasks: Sequence[SubTask],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> HierarchicalTask:
    return HierarchicalTask(
        name=name,
        subtasks=tuple(subtasks),
        metadata=dict(metadata or {}),
    )
