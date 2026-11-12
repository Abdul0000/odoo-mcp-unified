from pathlib import Path

from odoo_agent_server.services.module_analysis_service import ModuleAnalysisService


def _build_module(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "repo"
    module = project / "iot_device_management"
    for folder in ("models", "controllers", "security", "views"):
        (module / folder).mkdir(parents=True, exist_ok=True)
    (module / "__init__.py").write_text("from . import models, controllers\n", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'IoT', 'version': '18.0.1.0.0', 'depends': ['base'], "
        "'data': ['security/ir.model.access.csv', 'views/device_views.xml']}", encoding="utf-8"
    )
    (module / "models" / "__init__.py").write_text("from . import device\n", encoding="utf-8")
    (module / "models" / "device.py").write_text(
        "from odoo import fields, models\n"
        "class Device(models.Model):\n"
        "    _name = 'iot.device'\n"
        "    name = fields.Char()\n"
        "    def action_ping(self):\n"
        "        return True\n",
        encoding="utf-8",
    )
    (module / "controllers" / "__init__.py").write_text("from . import api\n", encoding="utf-8")
    (module / "controllers" / "api.py").write_text(
        "from odoo import http\n"
        "class Api(http.Controller):\n"
        "    @http.route('/iot/ping', type='json', auth='user')\n"
        "    def ping(self):\n"
        "        return {'ok': True}\n",
        encoding="utf-8",
    )
    (module / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_iot_device,iot.device,model_iot_device,base.group_user,1,1,1,0\n",
        encoding="utf-8",
    )
    (module / "views" / "device_views.xml").write_text(
        "<odoo><record id='view_iot_device_form' model='ir.ui.view'>"
        "<field name='name'>iot.device.form</field><field name='model'>iot.device</field>"
        "<field name='arch' type='xml'><form><field name='name'/></form></field>"
        "</record><menuitem id='menu_iot_root' name='IoT'/></odoo>",
        encoding="utf-8",
    )
    return project, module


def test_module_analysis_is_one_pass_and_returns_architecture(tmp_path):
    project, module = _build_module(tmp_path)
    result = ModuleAnalysisService().analyze(
        project=project,
        module_path=module,
        module_name="iot_device_management",
        analysis_goal="Explain architecture and practical gaps",
        max_files=50,
        max_total_chars=12000,
    )

    assert result["status"] == "analyzed"
    assert result["complete_for_analysis"] is True
    assert result["inventory"]["total_relevant_files"] == 8
    models = [model for item in result["architecture"]["python"] for model in item["models"]]
    routes = [route for item in result["architecture"]["python"] for route in item["routes"]]
    assert any(model["model"] == "iot.device" and "name" in model["fields"] for model in models)
    assert any("/iot/ping" in route["decorator"] for route in routes)
    assert result["next_call"] is None


def test_module_analysis_cache_invalidates_on_file_change(tmp_path):
    project, module = _build_module(tmp_path)
    service = ModuleAnalysisService()
    args = dict(
        project=project, module_path=module, module_name="iot_device_management",
        analysis_goal="Explain", max_files=50, max_total_chars=12000,
    )

    first = service.analyze(**args)
    second = service.analyze(**args)
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True

    device = module / "models" / "device.py"
    device.write_text(device.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
    third = service.analyze(**args)
    assert third["cache_hit"] is False


def test_module_analysis_prioritizes_explicit_target_and_search_match(tmp_path):
    project, module = _build_module(tmp_path)
    reports = module / "reports"
    reports.mkdir()
    report = reports / "custom_top_ten.py"
    report.write_text(
        "from odoo import models\n"
        "class CustomTopTen(models.AbstractModel):\n"
        "    _name = 'report.iot.top_ten'\n"
        "    _description = 'Custom Top Ten Report'\n",
        encoding="utf-8",
    )

    result = ModuleAnalysisService().analyze(
        project=project,
        module_path=module,
        module_name="iot_device_management",
        analysis_goal="Read the custom Top Ten report",
        max_files=5,
        max_total_chars=12000,
        target_files=["iot_device_management/reports/custom_top_ten.py"],
        search_patterns=["Top Ten", "top_ten"],
    )

    evidence = result["source_evidence"]
    assert evidence[0]["path"] == "reports/custom_top_ten.py"
    assert evidence[0]["selected_by"] == "explicit_target"
    assert result["selection"]["target_files_resolved"] == ["reports/custom_top_ten.py"]
    assert "reports/custom_top_ten.py" not in result["inventory"]["omitted_files"]


def test_module_analysis_search_hit_precedes_architecture_defaults(tmp_path):
    project, module = _build_module(tmp_path)
    reports = module / "reports"
    reports.mkdir()
    (reports / "ranking.py").write_text(
        "TITLE = 'Custom Top Ten Report'\n",
        encoding="utf-8",
    )

    result = ModuleAnalysisService().analyze(
        project=project,
        module_path=module,
        module_name="iot_device_management",
        analysis_goal="Locate the report",
        max_files=5,
        max_total_chars=12000,
        search_patterns=["Top Ten"],
    )

    assert result["source_evidence"][0]["path"] == "reports/ranking.py"
    assert result["source_evidence"][0]["selected_by"] == "search_match"
    assert result["selection"]["search_matched_files"] == ["reports/ranking.py"]


def test_module_analysis_cache_is_selector_aware_and_rejects_escaped_targets(tmp_path):
    project, module = _build_module(tmp_path)
    service = ModuleAnalysisService()
    base = dict(
        project=project,
        module_path=module,
        module_name="iot_device_management",
        analysis_goal="Focused read",
        max_files=5,
        max_total_chars=12000,
    )

    first = service.analyze(**base, target_files=["models/device.py"])
    cached = service.analyze(**base, target_files=["models/device.py"])
    different = service.analyze(**base, target_files=["views/device_views.xml"])
    escaped = service.analyze(**base, target_files=["../secret.py"])

    assert first["cache_hit"] is False
    assert cached["cache_hit"] is True
    assert different["cache_hit"] is False
    assert escaped["selection"]["target_files_invalid"] == ["../secret.py"]


def test_module_analysis_downranks_demo_noise_and_reserves_architecture_files(tmp_path):
    project, module = _build_module(tmp_path)
    (module / "data").mkdir()
    for index in range(12):
        (module / "data" / f"classroom_demo_{index}.xml").write_text(
            f"<odoo><record id='classroom_{index}' model='iot.device'/></odoo>",
            encoding="utf-8",
        )
    device = module / "models" / "device.py"
    device.write_text(device.read_text(encoding="utf-8") + "\nCLASSROOM = 'Classroom'\n", encoding="utf-8")

    result = ModuleAnalysisService().analyze(
        project=project,
        module_path=module,
        module_name="iot_device_management",
        analysis_goal="Analyze Classroom",
        max_files=15,
        max_total_chars=12000,
        search_patterns=["Classroom"],
    )

    selected = result["selection"]["selected_files"]
    assert selected[0] == "models/device.py"
    assert "__manifest__.py" in selected
    assert sum(path.startswith("data/") for path in selected) < 10
    assert "Classroom" in result["source_evidence"][0]["content"]


def test_module_analysis_discovers_direct_dependent_ui_owner(tmp_path):
    project, module = _build_module(tmp_path)
    portal = project / "iot_operations_portal"
    (portal / "views").mkdir(parents=True)
    (portal / "__manifest__.py").write_text(
        "{'name': 'IoT Portal', 'depends': ['iot_device_management'], 'data': ['views/device_views.xml']}",
        encoding="utf-8",
    )
    (portal / "views" / "device_views.xml").write_text(
        "<odoo><record id='action_devices' model='ir.actions.act_window'>"
        "<field name='res_model'>iot.device</field></record></odoo>",
        encoding="utf-8",
    )
    candidates = [
        type("Module", (), {"module_name": "iot_device_management", "path": "iot_device_management"})(),
        type("Module", (), {"module_name": "iot_operations_portal", "path": "iot_operations_portal"})(),
    ]

    result = ModuleAnalysisService().analyze(
        project=project,
        module_path=module,
        module_name="iot_device_management",
        analysis_goal="Analyze iot.device",
        max_files=15,
        max_total_chars=12000,
        target_files=["models/device.py"],
        search_patterns=["iot.device"],
        module_candidates=candidates,
    )

    contract = result["feature_contract"]
    assert contract["early_stop_reached"] is True
    assert contract["live_verification_needed"] is False
    assert contract["related_modules"][0]["module_name"] == "iot_operations_portal"
    assert contract["related_modules"][0]["reference_files"][0]["role"] == "ui"
