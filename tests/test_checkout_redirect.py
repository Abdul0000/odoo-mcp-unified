from __future__ import annotations

import unittest

from odoo_agent_server.services.checkout_redirect_service import build_ws_redirect


class CheckoutRedirectTests(unittest.TestCase):
    def test_redirect_points_at_ws_open_with_task_carried_over(self):
        out = build_ws_redirect(
            client_name="SproutBoxes",
            module_name="sproutboxes_product_design",
            task_description="Route Order Now to /shop/address",
            search_patterns=["buildCodButton", " ", "buildStickyBar"],
            target_files=["static/src/js/product_design.js"],
            commit_message="Route Order Now",
        )
        self.assertEqual(out["status"], "use_ws_lane")
        self.assertEqual(out["next_call"], {
            "tool": "ws.open",
            "arguments": {"client_name": "SproutBoxes", "module_name": "sproutboxes_product_design"},
        })
        self.assertEqual(out["carry_over"]["search_patterns"], ["buildCodButton", "buildStickyBar"])
        self.assertEqual(out["carry_over"]["commit_message"], "Route Order Now")

    def test_not_a_failure_and_not_complete(self):
        out = build_ws_redirect(client_name="c", module_name="m")
        self.assertFalse(out["protected_work_started"])
        self.assertFalse(out["success_allowed"])
        self.assertTrue(out["completion_required"])
        self.assertEqual(out["state_class"], "action_required")
        self.assertNotIn("error", out)

    def test_create_module_requests_new_workspace(self):
        out = build_ws_redirect(client_name="c", module_name="new_mod", create_module=True)
        self.assertEqual(out["next_call"]["arguments"]["create"], True)

    def test_message_tells_agent_to_continue_without_explaining(self):
        out = build_ws_redirect(client_name="c", module_name="m")
        self.assertIn("same turn", out["message"])
        self.assertIn("ws_edit", out["message"])


if __name__ == "__main__":
    unittest.main()


from odoo_agent_server.services.checkout_redirect_service import (  # noqa: E402
    CHECKOUT_REDIRECT_TOOLS,
    is_missing_checkout,
    redirect_from_arguments,
)


class GeneralRedirectTests(unittest.TestCase):
    def test_detects_both_missing_checkout_messages(self):
        self.assertTrue(is_missing_checkout("Project path not found: /workspace/repos/X"))
        self.assertTrue(is_missing_checkout("Existing checkout not found: /workspace/repos/X"))
        self.assertFalse(is_missing_checkout("module not found"))
        self.assertFalse(is_missing_checkout(None))

    def test_covers_every_source_reading_workflow(self):
        self.assertEqual(CHECKOUT_REDIRECT_TOOLS, {
            "workflow.odoo_code_change", "workflow.module_analyze",
            "workflow.odoo_debug_and_fix", "workflow.frontend_visual_implement",
        })

    def test_any_client_and_any_task_field_name(self):
        debug = redirect_from_arguments({"module_name": "kashees_reports", "issue_description": "wrong total"}, "Kashees 18")
        self.assertEqual(debug["next_call"]["arguments"], {"client_name": "Kashees 18", "module_name": "kashees_reports"})
        self.assertEqual(debug["carry_over"]["task_description"], "wrong total")
        visual = redirect_from_arguments({"module_name": "m", "design_goal": "hero banner", "create_module": True}, "Test 18")
        self.assertTrue(visual["next_call"]["arguments"]["create"])

    def test_no_module_means_no_redirect(self):
        self.assertIsNone(redirect_from_arguments({"task_description": "x"}, "c"))
        self.assertIsNone(redirect_from_arguments({"module_name": "m"}, ""))
