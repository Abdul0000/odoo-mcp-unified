from pathlib import Path
import shutil
import pytest

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def test_health():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_self_run_endpoint(monkeypatch, tmp_path):
    fixture_root = tmp_path
    shutil.copytree(Path(__file__).parent / "fixtures" / "test_module", fixture_root / "test_module",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(fixture_root))
    client = TestClient(app)
    response = client.post(
        "/qa/self-run",
        headers={"Authorization": "Bearer change-me"},
        json={
            "project_path": str(fixture_root),
            "module_name": "test_module",
            "run_pytest": False,
            "include_git_status": False,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "passed"
    assert data["summary"]["errors"] == 0


def test_self_run_defers_odoo_native_tests_instead_of_plain_pytest(monkeypatch, tmp_path):
    module = tmp_path / "native_test_module"
    tests_dir = module / "tests"
    tests_dir.mkdir(parents=True)
    (module / "__manifest__.py").write_text(
        "{'name': 'Native Test', 'version': '19.0.1.0.0', 'depends': ['base'], 'data': []}\n",
        encoding="utf-8",
    )
    (module / "__init__.py").write_text("", encoding="utf-8")
    (tests_dir / "__init__.py").write_text("from . import test_native\n", encoding="utf-8")
    (tests_dir / "test_native.py").write_text(
        "from odoo.tests.common import TransactionCase\n\n"
        "class TestNative(TransactionCase):\n"
        "    def test_native(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/qa/self-run",
        headers={"Authorization": "Bearer change-me"},
        json={
            "project_path": str(tmp_path),
            "module_name": "native_test_module",
            "run_pytest": True,
            "test_strategy": "auto",
            "include_git_status": False,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["summary"]["test_strategy"] == "odoo_native"
    assert data["status"] == "warning"
    assert data["summary"]["static_status"] == "passed"
    assert data["summary"]["runtime_test_status"] == "deferred"
    assert not data["commands"]
    assert "ODOO_NATIVE_TESTS_DEFERRED" in {item["code"] for item in data["findings"]}
    required = client.post(
        "/qa/self-run",
        headers={"Authorization": "Bearer change-me"},
        json={"project_path": str(tmp_path), "module_name": "native_test_module",
              "require_runtime_tests": True, "include_git_status": False},
    ).json()
    assert required["status"] == "failed"
    assert "REQUIRED_RUNTIME_TESTS_UNVERIFIED" in {item["code"] for item in required["findings"]}


def test_required_qa_never_uses_unrelated_project_tests(tmp_path, monkeypatch):
    from odoo_agent_server.routers.qa import self_run
    from odoo_agent_server.schemas import SelfRunRequest
    from odoo_agent_server.services.test_runner import TestRunner

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_other.py").write_text("def test_other(): assert True\n")
    addon = tmp_path / "target"
    addon.mkdir()
    (addon / "tests").mkdir()
    (addon / "__init__.py").write_text("")
    (addon / "__manifest__.py").write_text(repr({"name": "Target", "version": "19.0.1.0.0", "depends": ["base"]}))
    monkeypatch.setattr(settings, "allowed_roots_raw", str(tmp_path))
    monkeypatch.setattr(TestRunner, "pytest", lambda *a, **kw: pytest.fail("Unrelated tests must not run"))
    report = self_run(SelfRunRequest(project_path=str(tmp_path), module_name="target",
                                      include_git_status=False, require_runtime_tests=True))
    assert report.summary["runtime_test_status"] == "no_tests_found"
    assert report.status == "failed"


def test_isolated_pytest_requires_executed_assertions(tmp_path):
    import subprocess
    import sys
    from odoo_agent_server.services.test_runner import _PYTEST_CHECK

    test = tmp_path / "test_candidate.py"
    test.write_text("import pytest\n@pytest.mark.skip\ndef test_candidate(): pass\n")
    argv = [sys.executable, "-c", _PYTEST_CHECK, test.name]
    skipped = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)
    assert skipped.returncode == 5
    assert "ODOO_MCP_PYTEST_PASSED=0" in skipped.stdout
    test.write_text("def test_candidate(): assert True\n")
    passed = subprocess.run(argv, cwd=tmp_path, capture_output=True, text=True)
    assert passed.returncode == 0
    assert "ODOO_MCP_PYTEST_PASSED=1" in passed.stdout
