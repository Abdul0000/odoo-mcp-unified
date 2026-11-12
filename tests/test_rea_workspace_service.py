import unittest
from types import SimpleNamespace
from unittest.mock import patch

from odoo_agent_server.services.rea_upstream_service import ReaUpstreamService
from odoo_agent_server.services.rea_workspace_service import rea_workspace_snapshot
from tests.support.fake_github import FakeGitHub
from tests.test_scaffold_outline import make


class ReaWorkspaceSnapshotTests(unittest.TestCase):
    def test_snapshot_reads_manifest_and_current_workspace_files(self):
        _gh, _repo, service = make()
        ws_id = service.open("Acme", "my_mod")["ws_id"]
        edit = service.edit(ws_id, [{
            "path": "models/rea_snapshot_probe.py",
            "op": "write",
            "content": "from odoo import models\\n\\nclass ReaSnapshotProbe(models.Model):\\n    _name = 'rea.snapshot.probe'\\n",
        }])
        self.assertEqual(edit["status"], "ok", edit.get("findings"))

        with patch("odoo_agent_server.services.rea_workspace_service.workspace_service", return_value=service):
            with rea_workspace_snapshot(ws_id) as snapshot:
                self.assertEqual(snapshot["module_name"], "my_mod")
                self.assertIn("__manifest__.py", snapshot["files"])
                self.assertIn("models/rea_snapshot_probe.py", snapshot["files"])
                self.assertTrue((snapshot["module_path"] / "__manifest__.py").is_file())
                self.assertEqual(snapshot["source"]["provider"], "github_workspace")
                self.assertIsNotNone(snapshot["source"]["wip_branch"])
                self.assertTrue(snapshot["coverage"]["complete"])


class ReaGitHubUpstreamTests(unittest.TestCase):
    def test_compare_workspace_fetches_matching_odoo_series_from_github(self):
        core_files = {
            "addons/sale/__manifest__.py": "{'depends': ['base']}\n",
            "addons/sale/models/sale.py": (
                "from odoo import fields, models\n"
                "class SaleOrder(models.Model):\n"
                "    _name = 'sale.order'\n"
                "    x_partner_id = fields.Char()\n"
                "    def action_confirm(self):\n"
                "        return True\n"
            ),
            "odoo/addons/base/models/res_partner.py": (
                "from odoo import models\n"
                "class Partner(models.Model):\n"
                "    _name = 'res.partner'\n"
            ),
        }
        transport = FakeGitHub(core_files, branch="18.0")
        workspace = SimpleNamespace(
            repo=SimpleNamespace(token="test-token", transport=transport, sleep=lambda _s: None)
        )
        addon_files = {
            "__manifest__.py": "{'version': '18.0.1.0.0', 'depends': ['sale']}\n",
            "models/sale_order.py": (
                "from odoo import fields, models\n"
                "class SaleOrder(models.Model):\n"
                "    _inherit = 'sale.order'\n"
                "    x_partner_id = fields.Many2one('res.partner')\n"
                "    def action_confirm(self, force=False):\n"
                "        return True\n"
            ),
        }

        report = ReaUpstreamService().compare_workspace(
            files=addon_files, module_name="my_mod", workspace=workspace,
        )

        self.assertEqual(report["status"], "compared")
        self.assertEqual(report["series"], 18)
        self.assertEqual(report["upstream_source"], "odoo/odoo@18.0")
        codes = {item["code"] for item in report["findings"]}
        self.assertIn("METHOD_SIGNATURE_DRIFT", codes)
        self.assertIn("FIELD_TYPE_DRIFT", codes)
        self.assertTrue(report["coverage"]["complete"])
        self.assertTrue(any(url.endswith("/git/ref/heads/18.0") for _method, url in transport.calls))


if __name__ == "__main__":
    unittest.main()
