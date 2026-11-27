"""Durable coordinator contracts; existing tools retain their authorization."""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field
from .execution_schemas import AcceptedPlan


class TaskOpen(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_name: str
    request_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    description: str = Field(min_length=1, max_length=2000)
    target: Literal["coding", "push", "deploy", "functional"]
    module_name: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    risk_level: Literal["routine", "high"] | None = None
    accepted_plan: AcceptedPlan | None = None
    plan_approval_id: str | None = None


class TaskId(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class TaskRun(TaskId):
    step_id: str | None = Field(default=None, min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    tool: str | None = Field(default=None, min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=60)


class TaskList(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_name: str | None = None
    limit: int = Field(default=30, ge=1, le=100)


class TaskKnowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    client_name: str
    refresh: bool = False


class TaskTests(TaskId):
    apply: bool = False


class MutationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["create", "write", "action"]
    arguments: dict[str, Any] = Field(max_length=30)


class TaskDeploy(TaskId):
    module_name: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    step_id: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    module_root_rel: str | None = None
    approval_id: str | None = None
    verification_assertion: dict[str, Any] | None = None
    require_runtime_tests: bool = False
