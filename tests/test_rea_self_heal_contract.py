import pytest

from pydantic import ValidationError

from odoo_agent_server.schemas import ReaSelfHealRequest


def test_rea_self_heal_defaults_to_no_deployment():
    request = ReaSelfHealRequest(
        module_name="sales_custom",
        issue_description="The sale order total is incorrect after applying a discount.",
        expected_result="The order total must equal the discounted line totals.",
    )
    assert request.auto_deploy is False
    assert request.max_fix_cycles == 3
    assert request.action == "auto"


@pytest.mark.parametrize("module_name", ["../sales", "/tmp/addon", "sales\\custom", "bad\x00name"])
def test_rea_self_heal_rejects_unsafe_module_hints(module_name):
    with pytest.raises(ValidationError):
        ReaSelfHealRequest(
            module_name=module_name,
            issue_description="A reproducible issue exists.",
            expected_result="The expected result is restored.",
        )


def test_rea_self_heal_requires_concrete_expected_result():
    with pytest.raises(ValidationError):
        ReaSelfHealRequest(
            module_name="sales_custom",
            issue_description="Something is broken.",
            expected_result="x",
        )
