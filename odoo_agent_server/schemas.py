class WorkspaceRunRequest(BaseProjectRequest):
    """Fixed, bounded command in a checked-out local workspace."""
    command: Literal["git_status", "git_diff", "python_compile", "pytest", "ruff_check", "list_files"]
    relative_path: str = Field(default=".", max_length=240)
    pattern: str | None = Field(default=None, max_length=120)
    timeout_seconds: int = Field(default=60, ge=1, le=180)


class RepoListRequest(BaseProjectRequest):