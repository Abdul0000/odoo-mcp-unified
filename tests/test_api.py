from pathlib import Path
import shutil

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app


def test_health():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_self_run_endpoint(monkeypatch):
    fixture_root = Path(__file__).parent / "fixtures"
    # Other tests/compileall can create pycache under the fixture module; QA is
    # correct to warn on that in real addons, so keep this fixture clean first.
    shutil.rmtree(fixture_root / "test_module" / "models" / "__pycache__", ignore_errors=True)
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
    assert not data["commands"]
    assert "ODOO_NATIVE_TESTS_DEFERRED" in {item["code"] for item in data["findings"]}
