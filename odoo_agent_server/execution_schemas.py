"""Compact contracts for local task workspaces and the durable execution worker."""
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutionStatus(ExecutionRequest):
    operation_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class AcceptedPlan(ExecutionRequest):
    revision: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,100}$")
    base_sha: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    scope_paths: list[str] = Field(min_length=1, max_length=25)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=20)
    required_layers: list[Literal["syntax", "unit", "build", "orm", "integration"]] = Field(min_length=1, max_length=5)
    protected_inputs: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("scope_paths", "protected_inputs")
    @classmethod
    def repo_relative_paths(cls, values):
        paths = []
        for value in values:
            path = PurePosixPath(value.replace("\\", "/"))
            if not path.parts or path.is_absolute() or ":" in str(path) or "\0" in str(path) or {"..", ".git"}.intersection(path.parts):
                raise ValueError("Plan paths must be repository-relative files or directories")
            paths.append(path.as_posix())
        return list(dict.fromkeys(paths))

    @field_validator("acceptance_criteria")
    @classmethod
    def bounded_criteria(cls, values):
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("Acceptance criteria must be non-empty and at most 2000 characters each")
        return values


class WorkspaceOpen(ExecutionRequest):
    client_name: str | None = None
    project_path: str | None = None
    task_description: str = Field(min_length=1, max_length=12000)
    branch: str | None = Field(default=None, max_length=200)
    fresh: bool = True
    scope_paths: list[str] = Field(default_factory=list, max_length=25)
    accepted_plan: AcceptedPlan | None = None
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class WorkspaceId(ExecutionRequest):
    workspace_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class WorkspaceStatus(WorkspaceId):
    detail: Literal["full", "progress"] = Field(default="full", description="full verifies source; progress reads metadata and grants no completion proof.")


class TaskCheck(ExecutionRequest):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    argv: list[str] = Field(min_length=1, max_length=100)
    cwd: str = "."
    timeout_seconds: int = Field(default=120, ge=1, le=1800)
    layer: Literal["syntax", "unit", "build", "orm", "integration"] = "unit"
    output_dirs: list[str] = Field(default_factory=list, max_length=2)


class TaskPlan(WorkspaceId):
    checks: list[TaskCheck] = Field(min_length=1, max_length=12)
    skills: list[str] = Field(default_factory=list, max_length=5)
    max_repair_attempts: int = Field(default=3, ge=1, le=10)
    required_layers: list[Literal["syntax", "unit", "build", "orm", "integration"]] = Field(default_factory=lambda: ["unit"], min_length=1, max_length=5)
    replace_unavailable_checks: dict[str, str] = Field(default_factory=dict, max_length=12,
        description="Old check name to replacement name; only recorded runtime_unavailable checks may be replaced, preserving command arguments, cwd, layer and required layers; only Python interpreter selection may change.")
    replacement_reason: str | None = Field(default=None, min_length=20, max_length=2000)


class FileEdit(ExecutionRequest):
    operation: Literal["write", "replace", "delete", "rename"] = "replace"
    path: str
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    content: str | None = Field(default=None, max_length=500000)
    old_text: str | None = Field(default=None, min_length=1, max_length=500000)
    new_text: str | None = Field(default=None, max_length=500000)
    destination: str | None = None


class WorkspaceFiles(WorkspaceId):
    operation: Literal["list", "read", "write", "batch", "search", "diff"] = "list"
    path: str = "."
    content: str | None = Field(default=None, max_length=500000)
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    query: str | None = Field(default=None, min_length=1, max_length=1000)
    edits: list[FileEdit] | None = Field(default=None, min_length=1, max_length=25)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=16000, ge=1, le=32000)


class WorkspacePublish(WorkspaceId):
    message: str = Field(min_length=1, max_length=2000)
    branch: str | None = Field(default=None, max_length=200)
    push: bool = True
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    approval_id: str | None = None


class WorkspaceSync(WorkspaceId):
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    expected_remote_sha: str | None = Field(default=None, pattern=r"^[a-f0-9]{40,64}$")
    resolutions: dict[str, str | None] = Field(default_factory=dict, max_length=25)


class ProcessStart(WorkspaceId):
    argv: list[str] = Field(min_length=1, max_length=100)
    cwd: str = "."
    check_name: str | None = Field(default=None, max_length=80)
    timeout_seconds: int = Field(default=120, ge=1, le=1800)
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")


class ProcessId(ExecutionRequest):
    process_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class ProcessPoll(ProcessId):
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=16000, ge=1, le=32000)


class ProcessInput(ProcessId):
    data: str = Field(default="", max_length=16000)
    eof: bool = False


class AgentStart(WorkspaceId):
    task: str = Field(min_length=1, max_length=16000)
    request_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    max_turns: int = Field(default=20, ge=1, le=50)
    max_output_tokens: int = Field(default=4000, ge=256, le=8000)


class AgentId(ExecutionRequest):
    task_id: str = Field(pattern=r"^[a-f0-9]{32}$")
