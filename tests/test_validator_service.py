from pathlib import Path
import subprocess

from odoo_agent_server.schemas import Severity
from odoo_agent_server.services.validator_service import OdooModuleValidator


def test_valid_fixture_has_no_errors():
    module_path = Path(__file__).parent / "fixtures" / "test_module"
    findings = OdooModuleValidator().validate(module_path)
    errors = [item for item in findings if item.severity == Severity.error]
    assert errors == []


def test_missing_manifest_is_error(tmp_path):
    module_path = tmp_path / "broken_module"
    module_path.mkdir()
    findings = OdooModuleValidator().validate(module_path)
    assert any(item.code == "MISSING_MANIFEST" for item in findings)


def test_xml_parse_error_is_detected(tmp_path):
    module_path = tmp_path / "broken_xml"
    module_path.mkdir()
    (module_path / "__init__.py").write_text("", encoding="utf-8")
    (module_path / "__manifest__.py").write_text(
        "{'name': 'Broken XML', 'depends': ['base'], 'data': ['views/broken.xml']}",
        encoding="utf-8",
    )
    (module_path / "views").mkdir()
    (module_path / "views" / "broken.xml").write_text("<odoo><record></odoo>", encoding="utf-8")
    findings = OdooModuleValidator().validate(module_path)
    assert any(item.code == "XML_PARSE_ERROR" for item in findings)


def test_structure_gate_blocks_missing_import_acl_and_manifest_registration(tmp_path):
    module = tmp_path / "x_module"
    (module / "models").mkdir(parents=True)
    (module / "views").mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )
    (module / "models" / "__init__.py").write_text("", encoding="utf-8")
    (module / "models" / "asset.py").write_text(
        "from odoo import fields, models\n"
        "class Asset(models.Model):\n"
        "    _name = 'x.asset'\n"
        "    name = fields.Char()\n",
        encoding="utf-8",
    )
    (module / "views" / "asset_views.xml").write_text("<odoo/>", encoding="utf-8")

    findings = OdooModuleValidator().validate(module)
    codes = {finding.code for finding in findings if finding.severity == Severity.error}

    assert "PYTHON_IMPORT_CLOSURE_MISSING" in codes
    assert "ACCESS_ROW_MISSING_STATIC" in codes
    assert "MANIFEST_FILE_UNREGISTERED" in codes


def test_complete_module_contract_blocks_omitted_requested_layers(tmp_path):
    module = tmp_path / "solar_energy_management"
    module.mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'Solar', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )

    findings = OdooModuleValidator().validate(
        module,
        task_description="Build a complete Odoo module with models, security, views, reports, dashboards, automation, and demo data",
    )
    missing = [finding.message for finding in findings if finding.code == "REQUESTED_MODULE_LAYER_MISSING"]

    assert len(missing) == 7
    assert any(finding.code == "COMPLETE_MODULE_EMPTY" for finding in findings)


def test_access_reference_preserves_underscores_in_model_name(tmp_path):
    module = tmp_path / "x_module"
    (module / "models").mkdir(parents=True)
    (module / "security").mkdir()
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (module / "models" / "__init__.py").write_text("from . import asset_line\n", encoding="utf-8")
    (module / "models" / "asset_line.py").write_text(
        "from odoo import models\nclass AssetLine(models.Model):\n    _name = 'x.asset_line'\n",
        encoding="utf-8",
    )
    access = module / "security" / "ir.model.access.csv"
    access.write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n"
        "access_x_asset_line,x.asset_line,model_x_asset_line,base.group_user,1,1,1,0\n",
        encoding="utf-8",
    )
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': ['security/ir.model.access.csv']}",
        encoding="utf-8",
    )

    findings = OdooModuleValidator().validate(module)
    assert not any(finding.code == "ACCESS_ROW_MISSING_STATIC" for finding in findings)


def test_reverse_import_gate_blocks_imports_to_missing_packages(tmp_path):
    module = tmp_path / "iot_device_management"
    (module / "models").mkdir(parents=True)
    (module / "__init__.py").write_text(
        "from . import controllers\nfrom . import iot_handlers\nfrom . import models\nfrom . import wizard\n",
        encoding="utf-8",
    )
    (module / "models" / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'IoT', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )

    findings = OdooModuleValidator().validate(module)
    missing = {finding.message.rsplit(": ", 1)[-1] for finding in findings if finding.code == "PYTHON_IMPORT_TARGET_MISSING"}

    assert missing == {"controllers", "iot_handlers", "wizard"}


def test_controller_python_file_requires_controller_init(tmp_path):
    module = tmp_path / "iot_device_management"
    controllers = module / "controllers"
    controllers.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'IoT', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )
    (controllers / "api.py").write_text("from odoo import http\n", encoding="utf-8")

    findings = OdooModuleValidator().validate(module)
    missing = [finding for finding in findings if finding.code == "PYTHON_PACKAGE_INIT_MISSING"]

    assert len(missing) == 1
    assert missing[0].file == str(controllers / "__init__.py")


def test_tests_python_folder_also_requires_init(tmp_path):
    module = tmp_path / "iot_device_management"
    tests = module / "tests"
    tests.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'IoT', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )
    (tests / "test_device.py").write_text("def test_device():\n    assert True\n", encoding="utf-8")

    findings = OdooModuleValidator().validate(module)

    assert any(
        finding.code == "PYTHON_PACKAGE_INIT_MISSING" and finding.file == str(tests / "__init__.py")
        for finding in findings
    )


def test_import_only_and_placeholder_python_files_are_blocked(tmp_path):
    module = tmp_path / "x_module"
    models = module / "models"
    models.mkdir(parents=True)
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}", encoding="utf-8"
    )
    (models / "__init__.py").write_text("from . import empty, stub\n", encoding="utf-8")
    (models / "empty.py").write_text("from odoo import models\n", encoding="utf-8")
    (models / "stub.py").write_text("def unfinished():\n    pass\n", encoding="utf-8")

    findings = OdooModuleValidator().validate(module)
    codes = {finding.code for finding in findings}

    assert "IMPLEMENTATION_FILE_EMPTY" in codes
    assert "PLACEHOLDER_IMPLEMENTATION" in codes


def test_empty_xml_and_header_only_csv_are_blocked(tmp_path):
    module = tmp_path / "x_module"
    (module / "views").mkdir(parents=True)
    (module / "security").mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], "
        "'data': ['security/ir.model.access.csv', 'views/empty.xml']}", encoding="utf-8"
    )
    (module / "views" / "empty.xml").write_text("<odoo><data/></odoo>", encoding="utf-8")
    (module / "security" / "ir.model.access.csv").write_text(
        "id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink\n", encoding="utf-8"
    )

    findings = OdooModuleValidator().validate(module)
    empty_files = [finding for finding in findings if finding.code == "IMPLEMENTATION_FILE_EMPTY"]

    assert len(empty_files) == 2


def test_comment_only_frontend_asset_is_blocked(tmp_path):
    module = tmp_path / "x_module"
    assets = module / "static" / "src" / "js"
    assets.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], "
        "'data': [], 'assets': {'web.assets_backend': ['x_module/static/src/js/empty.js']}}",
        encoding="utf-8",
    )
    (assets / "empty.js").write_text("/** @odoo-module **/\n// TODO\n", encoding="utf-8")

    findings = OdooModuleValidator().validate(module)

    assert any(
        finding.code == "IMPLEMENTATION_FILE_EMPTY" and finding.file == str(assets / "empty.js")
        for finding in findings
    )


def test_requests_based_controller_is_blocked(tmp_path):
    module = tmp_path / "x_module"
    controllers = module / "controllers"
    controllers.mkdir(parents=True)
    (module / "__init__.py").write_text("from . import controllers\n", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}", encoding="utf-8"
    )
    (controllers / "__init__.py").write_text("from . import api\n", encoding="utf-8")
    (controllers / "api.py").write_text(
        "import requests\n\ndef heartbeat(url):\n    return requests.post(url).json()\n", encoding="utf-8"
    )

    findings = OdooModuleValidator().validate(module)
    codes = {finding.code for finding in findings}

    assert "CONTROLLER_REQUESTS_FORBIDDEN" in codes
    assert "ODOO_CONTROLLER_FRAMEWORK_MISSING" in codes


def test_complete_odoo_http_controller_passes_controller_gate(tmp_path):
    module = tmp_path / "x_module"
    controllers = module / "controllers"
    controllers.mkdir(parents=True)
    (module / "__init__.py").write_text("from . import controllers\n", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}", encoding="utf-8"
    )
    (controllers / "__init__.py").write_text("from . import api\n", encoding="utf-8")
    (controllers / "api.py").write_text(
        "from odoo import http\nfrom odoo.http import request\n\n"
        "class DeviceApi(http.Controller):\n"
        "    @http.route('/iot/device', type='json', auth='user')\n"
        "    def device(self):\n"
        "        return {'count': request.env['ir.model'].search_count([])}\n",
        encoding="utf-8",
    )

    findings = OdooModuleValidator().validate(module)
    codes = {finding.code for finding in findings}

    assert "CONTROLLER_REQUESTS_FORBIDDEN" not in codes
    assert "ODOO_CONTROLLER_FRAMEWORK_MISSING" not in codes


def test_existing_manifest_version_change_is_blocked(tmp_path):
    repo = tmp_path / "repo"
    module = repo / "x_module"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    manifest = module / "__manifest__.py"
    manifest.write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}", encoding="utf-8"
    )
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "baseline"], cwd=repo, check=True, capture_output=True)
    manifest.write_text(
        "{'name': 'X', 'version': '18.0.2.0.0', 'depends': ['base'], 'data': []}", encoding="utf-8"
    )

    findings = OdooModuleValidator().validate(module)

    assert any(finding.code == "MANIFEST_VERSION_CHANGED" for finding in findings)


def test_new_untracked_module_may_set_initial_manifest_version(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    module = repo / "new_module"
    module.mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'New', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}", encoding="utf-8"
    )

    findings = OdooModuleValidator().validate(module)

    assert not any(finding.code == "MANIFEST_VERSION_CHANGED" for finding in findings)


def test_detailed_iot_prompt_builds_enforceable_module_contract():
    prompt = """
    Develop a comprehensive Odoo module with all necessary files and deliverables.
    ### Model: `iot.box`
    ### Model: `iot.device`
    Implement controllers and endpoints POST `/iot/v1/heartbeat` and `/iot/v1/data/<token>`.
    Add an `iot_handlers` directory with Interface and Driver implementations.
    Build a wizard with `execute_command()`, implement `_sync_devices()` and `action_trigger()`.
    Add security groups, access rights and record rules, demo data, automation triggers, and unit tests.
    Create list view, form view, search view, kanban dashboard, menu items, and window actions.
    """

    contract = OdooModuleValidator.requested_contract(prompt, create_module=True)

    assert contract["full_module_requested"] is True
    assert set(contract["required_packages"]) == {"controllers", "iot_handlers", "wizard", "tests"}
    assert set(contract["required_models"]) == {"iot.box", "iot.device"}
    assert {"list", "form", "search", "kanban", "menu", "action"} <= set(contract["required_view_types"])
    assert {"security", "demo", "automation", "controllers", "handlers", "wizard", "tests"} <= set(contract["required_layers"])
    assert set(contract["required_methods"]) == {"_sync_devices", "action_trigger", "execute_command"}
    assert set(contract["required_routes"]) == {"/iot/v1/heartbeat", "/iot/v1/data/<token>"}


def test_session_note_pass_gate_does_not_require_a_new_tests_package():
    prompt = """
    Complete the Session Note flow:
    Appointment ID → GetAppointmentsByIdAsync → sessionNoteResponseId
    → clientId + isDraft → GetFormResponsesAsync → real record update.
    Existing browser cron must process pending appointments and mocked tests must pass.
    """

    contract = OdooModuleValidator.requested_contract(prompt)

    assert {
        "GetAppointmentsByIdAsync",
        "sessionNoteResponseId",
        "clientId",
        "isDraft",
        "GetFormResponsesAsync",
    } <= set(contract["required_symbols"])
    assert "tests" not in contract["required_layers"]
    assert "tests" not in contract["required_packages"]
    assert contract["required_test_symbols"] == []
    assert contract["tests_explicitly_requested"] is False


def test_preserved_layers_are_not_inferred_as_requested_deliverables():
    contract = OdooModuleValidator.requested_contract(
        "Add the Notes field and form view. Preserve existing security, access rights, "
        "record rules, actions, menus, controllers, and reports unchanged."
    )

    assert "views" in contract["required_layers"]
    assert "security" not in contract["required_layers"]
    assert "controllers" not in contract["required_layers"]
    assert "report" not in contract["required_layers"]
    assert "action" not in contract["required_view_types"]
    assert "menu" not in contract["required_view_types"]


def test_positive_layer_after_preservation_clause_is_still_requested():
    contract = OdooModuleValidator.requested_contract(
        "Preserve current security and add access rights for the new model."
    )

    assert "security" in contract["required_layers"]


def test_new_model_menu_prompt_builds_complete_small_feature_contract():
    contract = OdooModuleValidator.requested_contract(
        "Add a new clinic.test model with five fields, an internal-user ACL via "
        "ir.model.access, list and form views, a window action, and a Tests menu."
    )

    assert contract["required_models"] == ["clinic.test"]
    assert {"models", "security", "views"} <= set(contract["required_layers"])
    assert {"list", "form", "menu", "action"} <= set(contract["required_view_types"])


def test_session_note_contract_blocks_structural_only_implementation(tmp_path):
    module = tmp_path / "rethink"
    (module / "models").mkdir(parents=True)
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (module / "models" / "__init__.py").write_text("from . import rethink_session_note\n", encoding="utf-8")
    (module / "models" / "rethink_session_note.py").write_text(
        "from odoo import models\n\nclass SessionNote(models.Model):\n"
        "    _name = 'rethink.session.note'\n",
        encoding="utf-8",
    )
    (module / "__manifest__.py").write_text(
        "{'name': 'Rethink', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )
    prompt = (
        "Implement GetAppointmentsByIdAsync, sessionNoteResponseId, clientId, isDraft, "
        "GetFormResponsesAsync, and add mocked tests covering those identifiers."
    )

    findings = OdooModuleValidator().validate(module, task_description=prompt)
    requested_errors = {
        finding.code for finding in findings
        if finding.code.startswith("REQUESTED_")
    }

    assert "REQUESTED_SYMBOL_MISSING" in requested_errors
    assert "REQUESTED_MODULE_LAYER_MISSING" in requested_errors
    assert "REQUESTED_PACKAGE_MISSING" in requested_errors
    assert "REQUESTED_TEST_COVERAGE_MISSING" in requested_errors


def test_session_note_contract_accepts_named_flow_and_focused_tests(tmp_path):
    module = tmp_path / "rethink"
    (module / "models").mkdir(parents=True)
    (module / "tests").mkdir()
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    (module / "models" / "__init__.py").write_text("from . import rethink_session_note\n", encoding="utf-8")
    (module / "tests" / "__init__.py").write_text("", encoding="utf-8")
    symbols = [
        "GetAppointmentsByIdAsync",
        "sessionNoteResponseId",
        "clientId",
        "isDraft",
        "GetFormResponsesAsync",
    ]
    (module / "models" / "rethink_session_note.py").write_text(
        "from odoo import models\n\nclass SessionNote(models.Model):\n"
        "    _name = 'rethink.session.note'\n"
        f"    _flow_contract = {symbols!r}\n",
        encoding="utf-8",
    )
    (module / "tests" / "test_session_note.py").write_text(
        "def test_named_session_note_flow_contract():\n"
        f"    assert {symbols!r}\n",
        encoding="utf-8",
    )
    (module / "__manifest__.py").write_text(
        "{'name': 'Rethink', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )
    prompt = (
        "Implement GetAppointmentsByIdAsync, sessionNoteResponseId, clientId, isDraft, "
        "GetFormResponsesAsync, and add mocked tests covering those identifiers."
    )

    findings = OdooModuleValidator().validate(module, task_description=prompt)
    requested_errors = [
        finding for finding in findings
        if finding.code in {"REQUESTED_SYMBOL_MISSING", "REQUESTED_TEST_COVERAGE_MISSING"}
    ]

    assert requested_errors == []


def test_arbitrary_requested_python_package_is_discovered_dynamically():
    contract = OdooModuleValidator.requested_contract(
        "Create a new `device_plugins` directory with Python driver implementations.",
        create_module=True,
    )
    assert "device_plugins" in contract["required_packages"]


def test_unquoted_explicit_technical_folder_is_discovered_dynamically():
    contract = OdooModuleValidator.requested_contract(
        "Add a new device_plugins folder with the concrete drivers.",
        create_module=True,
    )
    assert "device_plugins" in contract["required_packages"]


def test_prose_words_are_not_inferred_as_python_packages():
    contract = OdooModuleValidator.requested_contract(
        "Keep the module root clean, implement this Python extension, and preserve the existing source package layout.",
        create_module=False,
    )
    assert contract["required_packages"] == []


def test_merge_contracts_removes_legacy_false_positive_packages():
    contract = OdooModuleValidator.merge_contracts({
        "required_packages": ["root", "extension", "python", "device_plugins"],
    })
    assert contract["required_packages"] == ["device_plugins"]


def test_arbitrary_xml_folder_and_static_assets_require_manifest_registration(tmp_path):
    module = tmp_path / "x_module"
    (module / "configuration").mkdir(parents=True)
    (module / "static" / "src" / "js").mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "configuration" / "device_views.xml").write_text("<odoo/>", encoding="utf-8")
    (module / "static" / "src" / "js" / "device.js").write_text("/** @odoo-module **/\n", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': [], 'assets': {}}",
        encoding="utf-8",
    )

    findings = OdooModuleValidator().validate(module)
    codes = {finding.code for finding in findings}

    assert "MANIFEST_FILE_UNREGISTERED" in codes
    assert "MANIFEST_ASSET_UNREGISTERED" in codes


def test_nested_python_packages_require_init_and_parent_imports(tmp_path):
    module = tmp_path / "x_module"
    plugins = module / "device_plugins"
    drivers = plugins / "drivers"
    drivers.mkdir(parents=True)
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'X', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )
    (plugins / "__init__.py").write_text("", encoding="utf-8")
    (drivers / "usb_driver.py").write_text("DRIVER = True\n", encoding="utf-8")

    findings = OdooModuleValidator().validate(module)
    codes = {finding.code for finding in findings}

    assert "PYTHON_PACKAGE_INIT_MISSING" in codes
    assert "PYTHON_IMPORT_CLOSURE_MISSING" in codes


def test_persisted_contract_is_enforced_when_followup_prompt_is_short(tmp_path):
    module = tmp_path / "iot_device_management"
    module.mkdir()
    (module / "__init__.py").write_text("", encoding="utf-8")
    (module / "__manifest__.py").write_text(
        "{'name': 'IoT', 'version': '18.0.1.0.0', 'depends': ['base'], 'data': []}",
        encoding="utf-8",
    )
    persisted = OdooModuleValidator.requested_contract(
        "Build a complete module with controllers, wizard, list view and form view.\n### Model: `iot.device`",
        create_module=True,
    )

    findings = OdooModuleValidator().validate(
        module,
        task_description="continue",
        module_contract=persisted,
    )
    codes = {finding.code for finding in findings if finding.severity == Severity.error}

    assert "REQUESTED_MODEL_MISSING" in codes
    assert "REQUESTED_PACKAGE_MISSING" in codes
    assert "REQUESTED_VIEW_TYPE_MISSING" in codes
