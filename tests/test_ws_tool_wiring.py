"""Static wiring checks for ws_* tools that run without the web framework installed."""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "odoo_agent_server"
TOOLS = (ROOT / "routers" / "tools.py").read_text()
MCP = (ROOT / "routers" / "mcp.py").read_text()
NAMES = ["ws.open", "ws.status", "ws.outline", "ws.scaffold", "ws.extend", "ws.symbol", "ws.read", "ws.grep", "ws.edit", "ws.diff", "ws.lint", "ws.commit", "ws.discard"]


class WsWiring(unittest.TestCase):
    def test_every_tool_is_registered_everywhere(self):
        for name in NAMES:
            self.assertIn(f'name="{name}"', TOOLS, f"descriptor missing for {name}")
            self.assertRegex(TOOLS, rf'"{re.escape(name)}": \(Ws\w+Request, ', f"handler row missing for {name}")
            self.assertIn(f'"{name}",', MCP, f"provider list missing {name}")

    def test_ws_names_are_strict_mcp_safe(self):
        for name in NAMES:
            self.assertRegex(re.sub(r"[^a-zA-Z0-9_-]", "_", name), r"^[a-zA-Z0-9_-]{1,64}$")

    def test_modules_parse(self):
        for path in ROOT.rglob("*.py"):
            ast.parse(path.read_text(), str(path))

    def test_next_call_target_is_a_provider_tool(self):
        self.assertIn('"workflow.module_install_and_check",', MCP)
        self.assertIn("workflow_module_install_and_check", (ROOT / "services" / "workspace_service.py").read_text())


if __name__ == "__main__":
    unittest.main()


def test_every_ws_descriptor_has_exactly_one_handler():
    from odoo_agent_server.routers import tools

    declared = {d.name for d in tools._TOOL_DESCRIPTORS if d.name.startswith("ws.")}
    assert declared == set(tools._WS_HANDLERS)
