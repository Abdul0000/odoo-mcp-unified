from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from odoo_agent_server.config import settings
from odoo_agent_server.services.repo_service import RepoService


def _addon(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    project = tmp_path / "repo"
    module = project / "device_module"
    module.mkdir(parents=True)
    (module / "__manifest__.py").write_text(
        "{'name': 'Device', 'version': '18.0.1.0.0', 'depends': ['base'], 'installable': True}\n",
        encoding="utf-8",
    )
    return project, module


def test_single_python_write_creates_and_propagates_package_imports(tmp_path, monkeypatch):
    project, module = _addon(tmp_path, monkeypatch)
    (module / "__init__.py").write_text("# root\n", encoding="utf-8")

    result = RepoService().write_file(
        str(project),
        "device_module/models/device.py",
        "from odoo import models\n\nclass Device(models.Model):\n    _name = 'device.test'\n",
    )

    assert (module / "models" / "__init__.py").read_text(encoding="utf-8") == "from . import device\n"
    assert (module / "__init__.py").read_text(encoding="utf-8") == "# root\nfrom . import models\n"
    assert result.extra["auto_wired_init_files"] == [
        "device_module/models/__init__.py",
        "device_module/__init__.py",
    ]
    assert "'version': '18.0.1.0.0'" in (module / "__manifest__.py").read_text(encoding="utf-8")


def test_nested_python_package_wiring_reaches_addon_root(tmp_path, monkeypatch):
    project, module = _addon(tmp_path, monkeypatch)
    (module / "__init__.py").write_text("", encoding="utf-8")

    RepoService().write_file(
        str(project), "device_module/controllers/api/device.py", "VALUE = 1\n"
    )

    assert (module / "controllers" / "api" / "__init__.py").read_text(encoding="utf-8") == "from . import device\n"
    assert (module / "controllers" / "__init__.py").read_text(encoding="utf-8") == "from . import api\n"
    assert (module / "__init__.py").read_text(encoding="utf-8") == "from . import controllers\n"


def test_tests_are_wired_inside_tests_but_not_imported_by_addon_root(tmp_path, monkeypatch):
    project, module = _addon(tmp_path, monkeypatch)
    (module / "__init__.py").write_text("from . import models\n", encoding="utf-8")

    RepoService().write_file(str(project), "device_module/tests/test_device.py", "def test_device():\n    assert True\n")

    assert (module / "tests" / "__init__.py").read_text(encoding="utf-8") == "from . import test_device\n"
    assert (module / "__init__.py").read_text(encoding="utf-8") == "from . import models\n"


def test_migration_python_write_is_not_auto_imported(tmp_path, monkeypatch):
    project, module = _addon(tmp_path, monkeypatch)
    (module / "__init__.py").write_text("", encoding="utf-8")

    result = RepoService().write_file(
        str(project), "device_module/migrations/18.0.1.0.1/post-migrate.py", "def migrate(cr, version):\n    pass\n"
    )

    assert "auto_wired_init_files" not in result.extra
    assert not (module / "migrations" / "__init__.py").exists()


def test_multi_write_merges_imports_without_duplicates(tmp_path, monkeypatch):
    project, module = _addon(tmp_path, monkeypatch)
    (module / "__init__.py").write_text("", encoding="utf-8")
    files = [
        SimpleNamespace(relative_path="device_module/models/device.py", content="VALUE = 1\n", create_dirs=True, mode="overwrite", force=False),
        SimpleNamespace(relative_path="device_module/models/command.py", content="VALUE = 2\n", create_dirs=True, mode="overwrite", force=False),
    ]

    result = RepoService().write_many_fast_safe(str(project), files)

    assert result.exit_code == 0
    models_init = (module / "models" / "__init__.py").read_text(encoding="utf-8")
    assert models_init.count("from . import command") == 1
    assert models_init.count("from . import device") == 1
    assert (module / "__init__.py").read_text(encoding="utf-8").count("from . import models") == 1


def test_multi_write_rejects_delete_of_auto_wired_init(tmp_path, monkeypatch):
    project, module = _addon(tmp_path, monkeypatch)
    (module / "__init__.py").write_text("", encoding="utf-8")
    files = [
        SimpleNamespace(
            relative_path="device_module/models/device.py",
            content="VALUE = 1\n",
            create_dirs=True,
            mode="overwrite",
            force=False,
        )
    ]

    with pytest.raises(HTTPException, match="cannot be written and deleted"):
        RepoService().write_many_fast_safe(
            str(project), files, delete_paths=["device_module/models/__init__.py"]
        )

    assert not (module / "models" / "device.py").exists()


def test_invalid_existing_init_rolls_back_primary_python_write(tmp_path, monkeypatch):
    project, module = _addon(tmp_path, monkeypatch)
    models = module / "models"
    models.mkdir()
    (models / "__init__.py").write_text("this is invalid python !!!\n", encoding="utf-8")

    with pytest.raises(HTTPException, match="auto-wiring failed"):
        RepoService().write_file(str(project), "device_module/models/device.py", "VALUE = 1\n")

    assert not (models / "device.py").exists()
    assert (models / "__init__.py").read_text(encoding="utf-8") == "this is invalid python !!!\n"


def test_atomic_patch_adds_python_package_wiring_in_same_transaction(tmp_path, monkeypatch):
    project, module = _addon(tmp_path, monkeypatch)
    (module / "__init__.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=project, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=project, check=True, capture_output=True)
    patch = """diff --git a/device_module/models/device.py b/device_module/models/device.py
new file mode 100644
--- /dev/null
+++ b/device_module/models/device.py
@@ -0,0 +1 @@
+VALUE = 1
"""

    result = RepoService().apply_patch_atomic(str(project), patch)

    assert result.exit_code == 0
    assert (module / "models" / "device.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert (module / "models" / "__init__.py").read_text(encoding="utf-8") == "from . import device\n"
    assert (module / "__init__.py").read_text(encoding="utf-8") == "from . import models\n"
