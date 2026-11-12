from types import SimpleNamespace

from odoo_agent_server.routers import workflow
from odoo_agent_server.schemas import CommandResult, SafeDeployModuleRequest


def _payload(**kwargs):
    values = {
        "client_name": "Cortex V18",
        "project_path": "/tmp/repo",
        "module_name": "cortex_account_reports_custom",
        "commit_message": "fix",
        "paths": ["cortex_account_reports_custom/models/top_customers_report.py"],
    }
    values.update(kwargs)
    return SafeDeployModuleRequest(**values)


def test_scoped_dirty_file_is_fresh_change(monkeypatch):
    monkeypatch.setattr(
        workflow.GitService,
        "changed_files",
        lambda self, project_path: CommandResult(
            command=["git", "changed-files"], cwd=project_path, exit_code=0,
            stdout="cortex_account_reports_custom/models/top_customers_report.py\n",
        ),
    )
    assert workflow._has_fresh_scoped_changes(_payload()) is True


def test_unrelated_dirty_file_does_not_bypass(monkeypatch):
    monkeypatch.setattr(
        workflow.GitService,
        "changed_files",
        lambda self, project_path: CommandResult(
            command=["git", "changed-files"], cwd=project_path, exit_code=0,
            stdout="other_module/models/x.py\n",
        ),
    )
    assert workflow._has_fresh_scoped_changes(_payload()) is False


def test_code_change_schema_exposes_force_new_deploy():
    from odoo_agent_server.schemas import OdooCodeChangeRequest

    req = OdooCodeChangeRequest(
        module_name="x_module",
        task_description="Push verified repair",
        force_new_deploy=True,
    )
    assert req.force_new_deploy is True
