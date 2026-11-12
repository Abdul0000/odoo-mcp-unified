from __future__ import annotations

from pathlib import Path

import pytest

from odoo_agent_server.schemas import OdooResult, OdooTargetResolutionHint
from odoo_agent_server.schemas import OdooCodeChangeRequest
from odoo_agent_server.routers import tools
from odoo_agent_server.services.existing_target_resolution_service import ExistingTargetResolutionService
from odoo_agent_server.services.odoo_jsonrpc_service import OdooJsonRpcService


@pytest.fixture(autouse=True)
def clear_resolution_cache():
    ExistingTargetResolutionService._cache.clear()
    yield
    ExistingTargetResolutionService._cache.clear()


def _module(tmp_path: Path, files: dict[str, str]) -> tuple[Path, Path, list[Path]]:
    project = tmp_path / "repo"
    module = project / "sample"
    module.mkdir(parents=True)
    paths = []
    for relative, content in files.items():
        path = module / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        paths.append(path)
    return project, module, paths


def _resolve(
    project: Path,
    module: Path,
    paths: list[Path],
    *,
    mode: str,
    kind: str,
    identifier: str,
    model: str | None = None,
    client=None,
):
    return ExistingTargetResolutionService().resolve(
        project=project,
        module_path=module,
        module_name="sample",
        task_description=f"Update existing {kind} {identifier}",
        target_hint=identifier,
        intent="modify_existing_feature" if mode == "existing" else "add_field",
        task_type="auto",
        change_request={},
        hint=OdooTargetResolutionHint(
            mode=mode,
            kind=kind,
            identifier=identifier,
            model=model,
        ),
        candidate_paths=paths,
        planned_paths=[],
        client=client,
        create_module=False,
    )


def test_existing_field_is_source_verified_without_forcing_live_odoo(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/res_partner.py": (
            "from odoo import fields, models\n"
            "class ResPartner(models.Model):\n"
            "    _inherit = 'res.partner'\n"
            "    x_note = fields.Char()\n"
        ),
    })

    result = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_note", model="res.partner",
    )

    assert result.status == "source_verified_live_unverified"
    assert result.source_verified is True
    assert result.live_checked is False
    assert result.live_status == "unavailable"
    assert result.writes_blocked is False
    assert result.matches[0].role == "definition"
    assert result.edit_policy.ownership == "client_custom"
    assert result.edit_policy.strategy == "direct_edit"
    assert result.edit_policy.direct_edit_paths == [str(paths[0])]


def test_odoo_core_target_requires_custom_inheritance(tmp_path):
    project, module, _ = _module(tmp_path, {
        "models/__init__.py": "",
    })
    core_path = tmp_path / "home" / "odoo" / "src" / "odoo" / "addons" / "sale" / "models" / "sale_order.py"
    core_path.parent.mkdir(parents=True)
    core_path.write_text(
        "from odoo import models\n"
        "class SaleOrder(models.Model):\n"
        "    _name = 'sale.order'\n"
        "    def action_confirm(self):\n"
        "        return True\n",
        encoding="utf-8",
    )

    result = _resolve(
        project, module, [core_path],
        mode="existing", kind="method", identifier="action_confirm", model="sale.order",
    )

    assert result.edit_policy.ownership == "odoo_core"
    assert result.edit_policy.strategy == "inherit_extension"
    assert result.edit_policy.protected_source_paths == [str(core_path)]


def test_other_addon_target_defaults_to_extension(tmp_path):
    project, module, _ = _module(tmp_path, {
        "models/__init__.py": "",
    })
    vendor_path = project / "vendor_addons" / "oca_sale" / "models" / "sale_order.py"
    vendor_path.parent.mkdir(parents=True)
    vendor_path.write_text(
        "from odoo import models\n"
        "class SaleOrder(models.Model):\n"
        "    _inherit = 'sale.order'\n"
        "    def action_confirm(self):\n"
        "        return super().action_confirm()\n",
        encoding="utf-8",
    )

    result = _resolve(
        project, module, [vendor_path],
        mode="existing", kind="method", identifier="action_confirm", model="sale.order",
    )

    assert result.edit_policy.ownership == "third_party"
    assert result.edit_policy.strategy == "inherit_extension"
    assert result.edit_policy.protected_source_paths == [str(vendor_path)]


def test_selected_custom_implementation_wins_over_upstream_definition(tmp_path):
    project, module, custom_paths = _module(tmp_path, {
        "models/sale_order.py": (
            "from odoo import models\n"
            "class SaleOrder(models.Model):\n"
            "    _inherit = 'sale.order'\n"
            "    def action_confirm(self):\n"
            "        return super().action_confirm()\n"
        ),
    })
    core_path = tmp_path / "home" / "odoo" / "src" / "odoo" / "addons" / "sale" / "models" / "sale_order.py"
    core_path.parent.mkdir(parents=True)
    core_path.write_text(
        "from odoo import models\n"
        "class SaleOrder(models.Model):\n"
        "    _name = 'sale.order'\n"
        "    def action_confirm(self):\n"
        "        return True\n",
        encoding="utf-8",
    )

    result = _resolve(
        project, module, [*custom_paths, core_path],
        mode="existing", kind="method", identifier="action_confirm", model="sale.order",
    )

    assert result.status != "ambiguous"
    assert result.edit_policy.strategy == "direct_edit"
    assert result.edit_policy.direct_edit_paths == [str(custom_paths[0])]


def test_locked_method_ignores_incidental_field_references_on_write_continuation(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/tebra_insurance.py": (
            "from odoo import fields, models\n"
            "class TebraSyncQueue(models.Model):\n"
            "    _name = 'tebra.sync.queue'\n"
            "    next_attempt_at = fields.Datetime()\n"
            "    started_at = fields.Datetime()\n"
            "    completed_at = fields.Datetime()\n"
            "    last_error = fields.Text()\n"
            "    attempts = fields.Integer()\n"
            "    state = fields.Selection([])\n"
            "    def enqueue(self, reset=False):\n"
            "        self.write({'next_attempt_at': False, 'started_at': False, "
            "'completed_at': False, 'last_error': False})\n"
        ),
    })
    task = (
        "Update existing method enqueue on tebra.sync.queue. Fresh pending jobs with "
        "attempts=0 make next_attempt_at due immediately; clear started_at, completed_at, "
        "and last_error. Preserve retry backoff and do not requeue done, failed, or skipped "
        "states unless reset=True."
    )
    result = ExistingTargetResolutionService().resolve(
        project=project,
        module_path=module,
        module_name="sample",
        task_description=task,
        target_hint="enqueue",
        intent="fix_bug",
        task_type="python_model",
        change_request={"method": "enqueue", "model": "tebra.sync.queue"},
        hint=OdooTargetResolutionHint(
            mode="existing",
            identifier="enqueue",
            model="tebra.sync.queue",
        ),
        candidate_paths=paths,
        planned_paths=["sample/models/tebra_insurance.py"],
        client=None,
        create_module=False,
    )

    assert result.writes_blocked is False
    assert result.status == "verified"
    assert [(match.kind, match.identifier, match.role) for match in result.matches] == [
        ("method", "enqueue", "definition")
    ]

    payload = OdooCodeChangeRequest(
        module_name="sample",
        project_path=str(project),
        task_description=task,
        target_hint="enqueue",
        intent="fix_bug",
    )
    locked = tools._lock_resolved_target(payload, result)
    continuation = tools._code_change_continuation_arguments(
        locked,
        client_name=None,
        project_path=str(project),
        module_name="sample",
        target_files=["sample/models/tebra_insurance.py"],
        preserve_writes=False,
    )
    assert continuation["target_resolution"] == {
        "mode": "existing",
        "kind": "method",
        "identifier": "enqueue",
        "model": "tebra.sync.queue",
    }


def test_ambiguous_existing_field_blocks_writes(tmp_path):
    source = (
        "from odoo import fields, models\n"
        "class Demo(models.Model):\n"
        "    _inherit = 'res.partner'\n"
        "    x_note = fields.Char()\n"
    )
    project, module, paths = _module(tmp_path, {
        "models/first.py": source,
        "models/second.py": source.replace("class Demo", "class DemoTwo"),
    })

    result = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_note", model="res.partner",
    )

    assert result.status == "ambiguous"
    assert result.writes_blocked is True
    assert "Multiple targets" in (result.question or "")


def test_missing_existing_target_blocks_writes(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/res_partner.py": "from odoo import models\nclass ResPartner(models.Model):\n    _inherit = 'res.partner'\n",
    })

    result = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_missing", model="res.partner",
    )

    assert result.status == "not_found"
    assert result.writes_blocked is True
    assert "exact technical identifier" in (result.question or "")


def test_broad_free_text_does_not_invent_a_technical_identifier(tmp_path):
    project, module, paths = _module(tmp_path, {
        "controllers/main.py": "from odoo import http\nclass Main(http.Controller):\n    pass\n",
    })

    result = ExistingTargetResolutionService().resolve(
        project=project,
        module_path=module,
        module_name="sample",
        task_description="Fix the existing controller bug",
        target_hint=None,
        intent="fix_bug",
        task_type="controller",
        change_request={},
        hint=OdooTargetResolutionHint(),
        candidate_paths=paths,
        planned_paths=[],
        client=None,
        create_module=False,
    )

    assert result.status == "scope_verified"
    assert result.identifier is None
    assert result.writes_blocked is False


def test_descriptive_target_hint_prefers_embedded_field_identifier(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/recon_session.py": (
            "from odoo import fields, models\n"
            "class ReconMatchLine(models.Model):\n"
            "    _name = 'aba.recon.match.line'\n"
            "    invoice_id = fields.Many2one('account.move')\n"
        ),
    })
    task = (
        "Fix existing field invoice_id on aba.recon.match.line so the Bill links "
        "to the existing invoice."
    )

    result = ExistingTargetResolutionService().resolve(
        project=project,
        module_path=module,
        module_name="sample",
        task_description=task,
        target_hint="aba.recon.match.line invoice_id / Bill linkage for existing created invoices",
        intent="fix_bug",
        task_type="python_model",
        change_request={},
        hint=OdooTargetResolutionHint(),
        candidate_paths=paths,
        planned_paths=[],
        client=None,
        create_module=False,
    )

    assert result.identifier == "invoice_id"
    assert result.model == "aba.recon.match.line"
    assert result.kind == "field"
    assert result.writes_blocked is False
    assert result.edit_policy.strategy == "direct_edit"


def test_qa_acronym_does_not_become_an_odoo_target(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/job.py": "from odoo import models\nclass Job(models.Model):\n    _name = 'sample.job'\n",
    })

    result = ExistingTargetResolutionService().resolve(
        project=project,
        module_path=module,
        module_name="sample",
        task_description="Apply the approved multi-file implementation and run QA before deployment.",
        target_hint=None,
        intent="implement",
        task_type="auto",
        change_request={},
        hint=OdooTargetResolutionHint(),
        candidate_paths=paths,
        planned_paths=["sample/models/job.py", "sample/views/job_views.xml"],
        client=None,
        create_module=False,
    )

    assert result.identifier is None
    assert result.writes_blocked is False


def test_target_clarification_continuation_preserves_supplied_writes():
    payload = OdooCodeChangeRequest(
        client_name="Anchor",
        project_path="/workspace/repos/anchor",
        module_name="tebra_integration",
        task_description="Apply approved module-wide implementation and QA.",
        operation_id="tebra-redesign-01",
        batch_index=0,
        total_batches=2,
        auto_deploy=False,
        deploy_mode="source_only",
        commit_message="Apply approved Tebra redesign",
        change_request={"scope": "module-wide", "qa_required": True},
        target_resolution={"mode": "existing", "kind": "other", "identifier": "tebra_integration"},
        writes=[
            {"relative_path": "tebra_integration/models/job.py", "content": "first"},
            {"relative_path": "tebra_integration/views/job_views.xml", "content": "second"},
        ],
    )

    arguments = tools._code_change_continuation_arguments(
        payload,
        client_name="Anchor",
        project_path="/workspace/repos/anchor",
        module_name="tebra_integration",
        target_files=["tebra_integration/models/job.py", "tebra_integration/views/job_views.xml"],
        preserve_writes=True,
    )

    assert arguments["writes"] == payload.model_dump(mode="json", exclude_none=True)["writes"]
    assert arguments["operation_id"] == "tebra-redesign-01"
    assert arguments["batch_index"] == 0
    assert arguments["total_batches"] == 2
    assert arguments["auto_deploy"] is False
    assert arguments["deploy_mode"] == "source_only"
    assert arguments["commit_message"] == "Apply approved Tebra redesign"
    assert arguments["change_request"]["qa_required"] is True
    assert arguments["target_resolution"]["identifier"] == "tebra_integration"
    assert "clarification_answer" not in arguments

    replacement = tools._code_change_continuation_arguments(
        payload,
        client_name="Anchor",
        project_path="/workspace/repos/anchor",
        module_name="tebra_integration",
        target_files=["tebra_integration/models/job.py"],
        preserve_writes=False,
        batch_index=1,
    )

    assert "writes" not in replacement
    assert replacement["batch_index"] == 1
    assert replacement["auto_deploy"] is False
    assert replacement["deploy_mode"] == "source_only"
    assert replacement["change_request"] == arguments["change_request"]
    assert replacement["target_resolution"] == arguments["target_resolution"]


def test_module_wide_redesign_does_not_treat_module_name_as_single_field(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/config.py": (
            "from odoo import fields, models\n"
            "class Config(models.Model):\n"
            "    _name = 'tebra.config'\n"
            "    tebra_integration = fields.Boolean()\n"
        ),
        "views/config_views.xml": (
            "<odoo><record id=\"tebra_integration\" model=\"ir.ui.view\">"
            "<field name=\"name\">tebra_integration</field></record></odoo>"
        ),
    })

    result = ExistingTargetResolutionService().resolve(
        project=project,
        module_path=module,
        module_name="tebra_integration",
        task_description="Complete module-wide redesign of tebra_integration with all models, views, sync logic, and tests.",
        target_hint="tebra_integration",
        intent="modify_existing_feature",
        task_type="auto",
        change_request={"scope": "module-wide"},
        hint=OdooTargetResolutionHint(),
        candidate_paths=paths,
        planned_paths=["sample/models/config.py", "sample/views/config_views.xml"],
        client=None,
        create_module=False,
    )

    assert result.status == "scope_verified"
    assert result.kind == "module_scope"
    assert result.writes_blocked is False
    assert result.live_checked is False


def test_git_branch_name_is_not_resolved_as_an_odoo_target(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/clinic_patient.py": (
            "from odoo import fields, models\n"
            "class ClinicPatient(models.Model):\n"
            "    _name = 'clinic.patient'\n"
            "    note = fields.Text()\n"
        ),
    })

    result = ExistingTargetResolutionService().resolve(
        project=project,
        module_path=module,
        module_name="sample",
        task_description="Add a Notes notebook page and push to test_18.",
        target_hint=None,
        intent=None,
        task_type="xml_view",
        change_request={},
        hint=OdooTargetResolutionHint(),
        candidate_paths=paths,
        planned_paths=["sample/models/clinic_patient.py"],
        client=None,
        create_module=False,
    )

    assert result.identifier is None
    assert result.status == "scope_verified"
    assert result.writes_blocked is False


def test_new_target_collision_blocks_but_absent_target_proceeds(tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/res_partner.py": (
            "from odoo import fields, models\n"
            "class ResPartner(models.Model):\n"
            "    _inherit = 'res.partner'\n"
            "    x_existing = fields.Char()\n"
        ),
    })

    collision = _resolve(
        project, module, paths,
        mode="new", kind="field", identifier="x_existing", model="res.partner",
    )
    absent = _resolve(
        project, module, paths,
        mode="new", kind="field", identifier="x_new", model="res.partner",
    )

    assert collision.status == "collision"
    assert collision.writes_blocked is True
    assert absent.status == "source_verified_live_unverified"
    assert absent.writes_blocked is False


def test_new_field_ignores_unrelated_installed_source_prose(tmp_path):
    project, module, custom_paths = _module(tmp_path, {
        "models/clinic_treatment.py": (
            "from odoo import fields, models\n"
            "class ClinicTreatment(models.Model):\n"
            "    _name = 'clinic.treatment'\n"
            "    name = fields.Char()\n"
        ),
    })
    mail_thread = (
        tmp_path / "home" / "odoo" / "src" / "odoo" / "addons" /
        "mail" / "models" / "mail_thread.py"
    )
    mail_thread.parent.mkdir(parents=True)
    mail_thread.write_text(
        "class MailThread:\n"
        "    help_text = 'Post a note in the chatter'\n",
        encoding="utf-8",
    )

    result = _resolve(
        project,
        module,
        [*custom_paths, mail_thread],
        mode="new",
        kind="field",
        identifier="note",
        model="clinic.treatment",
    )

    assert result.status == "source_verified_live_unverified"
    assert result.writes_blocked is False
    assert result.matches == []


def test_live_field_lookup_is_bounded_and_cached(monkeypatch, tmp_path):
    project, module, paths = _module(tmp_path, {
        "models/res_partner.py": (
            "from odoo import fields, models\n"
            "class ResPartner(models.Model):\n"
            "    _inherit = 'res.partner'\n"
            "    x_note = fields.Char()\n"
        ),
    })
    calls = []
    monkeypatch.setattr(OdooJsonRpcService, "configured", lambda self: True)

    def fields_get(self, model, timeout_seconds=60):
        calls.append((model, timeout_seconds))
        return OdooResult(status="ok", result={"x_note": {"type": "char"}})

    monkeypatch.setattr(OdooJsonRpcService, "fields_get", fields_get)
    client = object()

    first = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_note", model="res.partner", client=client,
    )
    second = _resolve(
        project, module, paths,
        mode="existing", kind="field", identifier="x_note", model="res.partner", client=client,
    )

    assert first.status == "verified"
    assert first.live_checked is True
    assert calls == [("res.partner", 5)]
    assert second.cache_hit is True
    assert calls == [("res.partner", 5)]
