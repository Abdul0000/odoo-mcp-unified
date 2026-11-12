from odoo_agent_server.routers.tools import list_tools
from odoo_agent_server.services.core_finder_service import CoreFinderService


def test_core_finder_default_priority_prefers_odoo_then_enterprise_then_user():
    result = CoreFinderService().find_core_file("point_of_sale", "any", "payment")

    assert result.searched_roots == [
        "/home/odoo/src/odoo/addons",
        "/home/odoo/src/enterprise",
        "/home/odoo/src/user",
    ]


def test_find_core_file_tool_description_does_not_advertise_user_first():
    tools = {tool.name: tool for tool in list_tools(strict_names=False)}
    description = tools["workflow.find_core_file"].description

    assert "/home/odoo/src/odoo/addons → /home/odoo/src/enterprise → /home/odoo/src/user" in description
    assert "/home/odoo/src/user → /home/odoo/src/enterprise" not in description
    assert "/home/odoo/src/enterprise → /home/odoo/src/odoo" not in description
