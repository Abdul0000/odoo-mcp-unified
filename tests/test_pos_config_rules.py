from odoo_agent_server.schemas import SyntaxVerifyRequest
from odoo_agent_server.services.syntax_verifier_service import SyntaxVerifierService


def test_pos_config_load_fields_is_blocked(tmp_path):
    js_file = tmp_path / "custom_pos" / "static" / "src" / "js" / "pos_config.js"
    js_file.parent.mkdir(parents=True)
    js_file.write_text(
        """/** @odoo-module **/
import { load_fields } from "@point_of_sale/app/store/models";

load_fields("pos.config", ["x_custom_field"]);
""",
        encoding="utf-8",
    )

    result = SyntaxVerifierService().verify(SyntaxVerifyRequest(written_files=[str(js_file)], project_path=str(tmp_path)))

    assert result.push_blocked is True
    assert any(f.check == "pos_config_no_load_fields" for f in result.findings)


def test_pos_config_load_pos_data_fields_override_is_blocked(tmp_path):
    py_file = tmp_path / "custom_pos" / "models" / "pos_config.py"
    py_file.parent.mkdir(parents=True)
    py_file.write_text(
        """from odoo import models


class PosConfig(models.Model):
    _inherit = "pos.config"

    def _load_pos_data_fields(self, config_id):
        return super()._load_pos_data_fields(config_id) + ["x_custom_field"]
""",
        encoding="utf-8",
    )

    result = SyntaxVerifierService().verify(
        SyntaxVerifyRequest(written_files=[str(py_file)], project_path=str(tmp_path))
    )

    assert result.push_blocked is True
    assert any(
        finding.check == "pos_config_no_load_pos_data_fields"
        for finding in result.findings
    )


def test_other_pos_model_load_pos_data_fields_is_not_blocked_by_pos_config_rule(tmp_path):
    py_file = tmp_path / "custom_pos" / "models" / "pos_order.py"
    py_file.parent.mkdir(parents=True)
    py_file.write_text(
        """from odoo import models


class PosOrder(models.Model):
    _inherit = "pos.order"

    def _load_pos_data_fields(self, config_id):
        return super()._load_pos_data_fields(config_id)
""",
        encoding="utf-8",
    )

    result = SyntaxVerifierService().verify(
        SyntaxVerifyRequest(written_files=[str(py_file)], project_path=str(tmp_path))
    )

    assert not any(
        finding.check == "pos_config_no_load_pos_data_fields"
        for finding in result.findings
    )
