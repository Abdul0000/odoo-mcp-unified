from pathlib import Path
import pytest

from odoo_agent_server.services.deploy_impact_service import classify_deploy_impact


def test_static_scss_skips_upgrade(tmp_path: Path):
    result = classify_deploy_impact(project_path=tmp_path, module_name="x_mod", changed_paths=["x_mod/static/src/css/a.scss"], git_diff="", deploy_mode="auto")
    assert result.impact == "asset_only"
    assert result.upgrade_required is False


def test_backend_xml_requires_upgrade(tmp_path: Path):
    result = classify_deploy_impact(project_path=tmp_path, module_name="x_mod", changed_paths=["x_mod/views/a.xml"], git_diff="", deploy_mode="auto")
    assert result.upgrade_required is True


def test_python_method_body_skips_upgrade(tmp_path: Path):
    diff = """diff --git a/x_mod/models/a.py b/x_mod/models/a.py\n--- a/x_mod/models/a.py\n+++ b/x_mod/models/a.py\n@@ -1 +1 @@\n-        return 1\n+        return 2\n"""
    result = classify_deploy_impact(project_path=tmp_path, module_name="x_mod", changed_paths=["x_mod/models/a.py"], git_diff=diff, deploy_mode="auto")
    assert result.impact == "python_restart_only"
    assert result.upgrade_required is False


def test_python_field_change_requires_upgrade(tmp_path: Path):
    diff = """diff --git a/x_mod/models/a.py b/x_mod/models/a.py\n--- a/x_mod/models/a.py\n+++ b/x_mod/models/a.py\n@@ -1 +1 @@\n+    name = fields.Char(required=True)\n"""
    result = classify_deploy_impact(project_path=tmp_path, module_name="x_mod", changed_paths=["x_mod/models/a.py"], git_diff=diff, deploy_mode="auto")
    assert result.upgrade_required is True


def test_source_only_cannot_bypass_structural_change(tmp_path: Path):
    with pytest.raises(ValueError):
        classify_deploy_impact(project_path=tmp_path, module_name="x_mod", changed_paths=["x_mod/__manifest__.py"], git_diff="", deploy_mode="source_only")
