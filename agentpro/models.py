from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class AgentState(str, Enum):
    IDLE = "IDLE"
    DECOMPOSE = "DECOMPOSE"
    OBSERVE = "OBSERVE"
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    VERIFY_ACTION = "VERIFY_ACTION"
    VERIFY_GOAL = "VERIFY_GOAL"
    NEED_CONFIRMATION = "NEED_CONFIRMATION"
    RECOVER = "RECOVER"
    REPLAN = "REPLAN"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    STUCK = "STUCK"


class ActionType(str, Enum):
    OPEN_APP = "open_app"
    TAP = "tap"
    BACK = "back"
    SCREENSHOT = "screenshot"
    UI_DUMP = "ui_dump"
    WAIT = "wait"
    FINISH = "finish"
    INSTALL_APK = "install_apk"
    INPUT_TEXT = "input_text"
    SWIPE = "swipe"
    OPEN_URL = "open_url"
    KEY_EVENT = "key_event"
    LONG_PRESS = "long_press"
    CLEAR_TEXT = "clear_text"
    ERASE_TEXT = "erase_text"
    GET_WINDOW = "get_window"


@dataclass(frozen=True)
class AgentAction:
    action_type: ActionType
    arguments: Mapping[str, Any] = field(default_factory=dict)
    requires_confirmation: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.action_type, ActionType):
            raise TypeError("action_type must be an ActionType")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("arguments must be a mapping")
        if not isinstance(self.requires_confirmation, bool):
            raise TypeError("requires_confirmation must be bool")


@dataclass(frozen=True)
class Observation:
    success: bool
    package_name: str | None = None
    activity_name: str | None = None
    ui_root: Mapping[str, Any] | None = None
    screenshot_base64: str | None = None
    fingerprint: str | None = None
    tree_truncated: bool = False
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ActionResult:
    success: bool
    operation_id: int | None = None
    data: Mapping[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class GoalResult:
    success: bool
    reason: str


@dataclass
class AgentContext:
    goal: str
    state: AgentState = AgentState.IDLE
    step: int = 0
    recovery_count: int = 0
    replan_count: int = 0
    action_count: int = 0
    last_fingerprint: str | None = None
    current_fingerprint: str | None = None
    last_action: AgentAction | None = None
    last_action_result: ActionResult | None = None
    observation: Observation | None = None
    plan: list[AgentAction] = field(default_factory=list)
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if not isinstance(self.goal, str):
            raise TypeError("goal must be a string")
        if not self.goal.strip():
            raise ValueError("goal must not be empty")
        if self.step < 0:
            raise ValueError("step must not be negative")
        if self.recovery_count < 0:
            raise ValueError("recovery_count must not be negative")
        if self.replan_count < 0:
            raise ValueError("replan_count must not be negative")
        if self.action_count < 0:
            raise ValueError("action_count must not be negative")
