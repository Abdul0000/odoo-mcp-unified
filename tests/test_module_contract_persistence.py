from odoo_agent_server.routers.tools import (
    _persist_module_contract,
    _update_module_contract_status,
)


class MemoryStore:
    def __init__(self):
        self.state = {}

    def load_session_state(self, client_name):
        return dict(self.state)

    def save_session_state(self, state, client_name=None):
        self.state.update(state)
        return dict(self.state)


def test_module_contract_survives_short_followups_but_resets_for_new_task(tmp_path):
    store = MemoryStore()
    original = {
        "full_module_requested": True,
        "required_packages": ["controllers", "wizard"],
        "required_models": ["iot.device"],
    }
    merged, first = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="iot_device_management",
        current_contract=original,
        task_description="Build the complete IoT module with all requested device fields and views.",
    )
    assert first["status"] == "pending"
    assert merged["required_packages"] == ["controllers", "wizard"]

    _update_module_contract_status(
        store,
        client_name="odoo 18",
        module_name="iot_device_management",
        status="fulfilled",
    )
    retained, second = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="iot_device_management",
        current_contract={},
        task_description="continue",
    )
    assert second["status"] == "fulfilled"
    assert retained["required_models"] == ["iot.device"]
    assert second["requirement_sources"] == ["Build the complete IoT module with all requested device fields and views."]

    independent, third = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="iot_device_management",
        current_contract={"required_layers": ["models"]},
        task_description="Swap two field labels and calculate value from units.",
    )
    assert third["status"] == "pending"
    assert independent["required_layers"] == ["models"]
    assert independent["required_models"] == []
    assert independent["required_packages"] == []
    assert third["requirement_sources"] == ["Swap two field labels and calculate value from units."]


def test_module_contract_merges_explicit_continuation(tmp_path):
    store = MemoryStore()
    original = {
        "full_module_requested": True,
        "required_layers": ["models", "report"],
        "required_models": ["iot.device"],
    }
    _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="iot_device_management",
        current_contract=original,
        task_description="Build the complete IoT module with a status report.",
    )

    expanded, record = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="iot_device_management",
        current_contract={"required_methods": ["execute_command"]},
        task_description="Additionally add execute_command to the same task.",
    )

    assert record["status"] == "pending"
    assert expanded["required_layers"] == ["models", "report"]
    assert expanded["required_models"] == ["iot.device"]
    assert expanded["required_methods"] == ["execute_command"]


def test_pending_contract_does_not_contaminate_unrelated_surgical_task(tmp_path):
    store = MemoryStore()
    _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="rethink_bh_connector",
        current_contract={"required_layers": ["report"], "required_view_types": ["form"]},
        task_description="Build an operational report and report form.",
    )

    surgical, record = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="rethink_bh_connector",
        current_contract={"required_layers": ["models"]},
        task_description="Swap Hours and Units labels and calculate Value from units.",
    )

    assert surgical["required_layers"] == ["models"]
    assert surgical["required_view_types"] == []
    assert record["requirement_sources"] == ["Swap Hours and Units labels and calculate Value from units."]


def test_legacy_pass_gate_drops_stale_tests_package_on_continuation(tmp_path):
    store = MemoryStore()
    store.state = {
        "module_contracts": {
            "rethink": {
                "project_path": str(tmp_path.resolve()),
                "module_name": "rethink",
                "contract": {
                    "required_packages": ["tests"],
                    "required_layers": ["tests"],
                    "required_test_symbols": ["GetAppointmentsByIdAsync"],
                },
                "requirement_sources": [
                    "Implement GetAppointmentsByIdAsync; existing mocked tests must pass."
                ],
                "task_description": "Implement GetAppointmentsByIdAsync; existing mocked tests must pass.",
                "status": "pending",
            }
        }
    }

    merged, _record = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="rethink",
        current_contract={},
        task_description="deploy",
    )

    assert "tests" not in merged["required_packages"]
    assert "tests" not in merged["required_layers"]
    assert merged["required_test_symbols"] == []
    assert merged["tests_explicitly_requested"] is False


def test_legacy_explicit_test_request_remains_blocking_on_continuation(tmp_path):
    store = MemoryStore()
    store.state = {
        "module_contracts": {
            "rethink": {
                "project_path": str(tmp_path.resolve()),
                "module_name": "rethink",
                "contract": {
                    "required_packages": ["tests"],
                    "required_layers": ["tests"],
                    "required_test_symbols": ["GetAppointmentsByIdAsync"],
                },
                "requirement_sources": [
                    "Add mocked tests covering GetAppointmentsByIdAsync."
                ],
                "task_description": "Add mocked tests covering GetAppointmentsByIdAsync.",
                "status": "pending",
            }
        }
    }

    merged, _record = _persist_module_contract(
        store,
        client_name="odoo 18",
        project_path=str(tmp_path),
        module_name="rethink",
        current_contract={},
        task_description="continue",
    )

    assert "tests" in merged["required_packages"]
    assert "tests" in merged["required_layers"]
    assert merged["required_test_symbols"] == ["GetAppointmentsByIdAsync"]
    assert merged["tests_explicitly_requested"] is True
