from pathlib import Path

from fastapi.testclient import TestClient

from odoo_agent_server.config import settings
from odoo_agent_server.main import app
from odoo_agent_server.services.skill_service import SkillService


EXPECTED = {
    "odoo-accounting-development": "01-SKILL-accounting.md",
    "odoo-manufacturing-development": "02-SKILL-manufacturing.md",
    "odoo-inventory-development": "03-SKILL-inventory.md",
    "odoo-sales-development": "04-SKILL-sales.md",
    "odoo-purchase-development": "05-SKILL-purchase.md",
    "odoo-hr-payroll-development": "06-SKILL-hr-payroll.md",
    "odoo-crm-pipeline-development": "07-SKILL-crm-pipeline.md",
    "odoo-helpdesk-development": "08-SKILL-helpdesk.md",
    "odoo-sql-query-development": "09-SKILL-sql-queries.md",
    "odoo-all-modules-reference": "10-ALL-MODULES-REFERENCE.md",
    "odoo-qweb-reports-development": "11-SKILL-qweb-reports-development.md",
    "odoo-accounting-reports": "12-SKILL-accounting-reports.md",
    "odoo-pos-development": "odoo-pos-development-skill.md",
    "odoo-website-development": "odoo-website-development.md",
    "odoo-custom-module-development": "13-SKILL-custom-module.md",
    "odoo-task-planning": "14-SKILL-planning.md",
    "odoo-view-development": "15-SKILL-views.md",
    "odoo-backend-development": "16-SKILL-backend.md",
    "odoo-frontend-owl-development": "17-SKILL-frontend-owl.md",
    "odoo-integration-development": "18-SKILL-integration.md",
    "odoo-security-development": "19-SKILL-security.md",
    "odoo-runtime-debugging": "20-SKILL-runtime-debugging.md",
    "odoo-test-development": "21-SKILL-tests.md",
    "odoo-code-review": "22-SKILL-code-review.md",
    "odoo-upgrade-migration": "23-SKILL-upgrade-migration.md",
    "odoo-ai-mcp-development": "24-SKILL-ai-mcp.md",
    "odoo-functional-flow-testing": "25-SKILL-functional-flow-testing.md",
}


def test_exact_skill_files_are_loaded_only_on_demand():
    root = Path(__file__).resolve().parents[1] / "skills"
    service = SkillService()
    listed = service.list()

    assert listed["loading"] == "on_demand"
    assert listed["selection_policy"] == "single_primary_owner"
    assert listed["count"] == len(EXPECTED)
    assert {item["name"] for item in listed["skills"]} == set(EXPECTED)

    for name, filename in EXPECTED.items():
        result = service.read(name)
        assert result["exact"] is True
        assert result["source"] == f"skills/{filename}"
        assert result["content"] == (root / filename).read_text(encoding="utf-8")


def test_provider_can_list_and_read_exact_skill_resource():
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {settings.api_token}"}
    listed = client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200
    names = {tool["name"] for tool in listed.json()["result"]["tools"]}
    assert {"skills_list", "skills_read"} <= names

    read = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "skills_read", "arguments": {"name": "accounting"}},
        },
    )
    assert read.status_code == 200
    result = read.json()["result"]["structuredContent"]
    expected = (Path(__file__).resolve().parents[1] / "skills" / EXPECTED["odoo-accounting-development"]).read_text(encoding="utf-8")
    assert result["name"] == "odoo-accounting-development"
    assert result["content"] == expected
    assert result["exact"] is True


def test_query_reporting_and_module_reference_aliases_load_on_demand():
    service = SkillService()

    reporting = service.read("reporting")
    modules = service.read("modules")

    assert reporting["name"] == "odoo-sql-query-development"
    assert reporting["source"] == "skills/09-SKILL-sql-queries.md"
    assert modules["name"] == "odoo-all-modules-reference"
    assert modules["source"] == "skills/10-ALL-MODULES-REFERENCE.md"


def test_generic_report_alias_uses_qweb_while_reporting_stays_query_focused():
    service = SkillService()

    assert service.read("report")["name"] == "odoo-qweb-reports-development"
    assert service.read("reports")["name"] == "odoo-qweb-reports-development"
    assert service.read("reporting")["name"] == "odoo-sql-query-development"


def test_skill_list_recommends_without_loading_skill_content():
    service = SkillService()

    accounting = service.list("Customize the balance sheet accounting report")
    qweb = service.list("Create a QWeb PDF report template")
    query = service.list("Use read_group to return record counts by state")

    assert accounting["recommendation"]["name"] == "odoo-accounting-reports"
    assert qweb["recommendation"]["name"] == "odoo-qweb-reports-development"
    assert query["recommendation"]["name"] == "odoo-sql-query-development"
    assert all("content" not in result for result in (accounting, qweb, query))


def test_qweb_report_aliases_load_dedicated_report_skill():
    service = SkillService()

    for alias in ("qweb", "qweb-report", "pdf-report", "report-template"):
        result = service.read(alias)
        assert result["name"] == "odoo-qweb-reports-development"
        assert result["source"] == "skills/11-SKILL-qweb-reports-development.md"


def test_accounting_report_aliases_load_dedicated_accounting_report_skill():
    service = SkillService()

    for alias in ("accounting-report", "financial-report", "invoice-report", "ar-aging", "tax-report"):
        result = service.read(alias)
        assert result["name"] == "odoo-accounting-reports"
        assert result["source"] == "skills/12-SKILL-accounting-reports.md"


def test_interactive_account_report_contract_routes_to_accounting_skill():
    service = SkillService()

    for task in (
        "Add an AccountReportFilters Top N filter",
        "Fix account_reports updateOption behavior",
        "Persist account report options correctly",
    ):
        assert service.list(task)["recommendation"]["name"] == "odoo-accounting-reports"

    content = service.read("accounting-report")["content"]
    for marker in (
        "ir.actions.client.params",
        'options` as a mutable mapping',
        'updateOption("top_x", topX, true)',
        "explicit current request override",
        "browser asset URL/hash",
    ):
        assert marker in content


def test_view_aliases_and_surface_routing_load_dedicated_view_skill():
    service = SkillService()

    for alias in ("view", "views", "xpath", "xml-view", "backend-view"):
        result = service.read(alias)
        assert result["name"] == "odoo-view-development"
        assert result["source"] == "skills/15-SKILL-views.md"

    recommended = service.list("Add a field to the sale order form view with a stable xpath")
    assert recommended["recommendation"]["name"] == "odoo-view-development"
    content = service.read("view")["content"]
    for marker in (
        "owning addon", "priority", "representative users", "noupdate",
        "Studio", "t-call", "final compiled architecture",
        "t-on-keydown.enter", "duplicate component registration",
    ):
        assert marker in content


def test_planning_skill_rejects_diagnostic_scope_invention():
    content = SkillService().read("planning")["content"]

    for marker in (
        "evidence, not automatic requirements",
        "Never bypass a false-positive contract",
        "connector_pipeline",
        "unterminated quoted string",
        "nonblocking baseline debt",
    ):
        assert marker in content


def test_unique_technical_skill_owners_route_on_demand():
    service = SkillService()
    cases = {
        "Add a computed field with an _inherit model and batch method": "odoo-backend-development",
        "Build an OWL component client action with a registry category": "odoo-frontend-owl-development",
        "Implement an external API webhook with rate limit handling": "odoo-integration-development",
        "Fix an ir.rule record rule cross company access leak": "odoo-security-development",
        "The cron failed with a runtime error traceback": "odoo-runtime-debugging",
        "Add a SavepointCase regression test": "odoo-test-development",
        "Perform a production readiness code review": "odoo-code-review",
        "Migrate from Odoo 17 to Odoo 19 with deprecated API changes": "odoo-upgrade-migration",
        "Create an Odoo MCP tool for a RAG pipeline": "odoo-ai-mcp-development",
    }

    for task, expected in cases.items():
        result = service.list(task)
        assert result["recommendation"]["name"] == expected
        assert result["recommendation"]["selection_policy"] == "single_primary_owner"
        assert "content" not in result["recommendation"]


def test_specific_existing_owner_beats_cross_layer_generic_terms():
    service = SkillService()

    accounting = service.list("Add an OWL Top N account report filter in account_reports")
    view = service.list("Add a field to a form view with an inherited view and xpath")

    assert accounting["recommendation"]["name"] == "odoo-accounting-reports"
    assert view["recommendation"]["name"] == "odoo-view-development"


def test_skill_registry_has_one_file_per_authoritative_owner():
    service = SkillService()
    rows = [service.read(name) for name in service.names()]

    assert len({row["name"] for row in rows}) == len(rows)
    assert len({row["source"] for row in rows}) == len(rows)
    assert service.read("orm")["name"] == "odoo-sql-query-development"
    assert service.read("backend")["name"] == "odoo-backend-development"
    assert service.read("debug")["name"] == "odoo-runtime-debugging"


def test_new_skills_preserve_existing_composite_workflow_ownership():
    service = SkillService()

    debug = service.read("debug")["content"]
    ai_mcp = service.read("mcp")["content"]
    planning = service.read("planning")["content"]

    assert "workflow.odoo_debug_and_fix" in debug
    assert "existing code-change, QA, safe-deploy, resume, and replay flow" in debug
    assert "never create a parallel deployment pipeline" in ai_mcp
    assert "normally 3–8 relevant files" in planning


def test_vetted_expert_deltas_stay_compact_and_in_existing_owners():
    service = SkillService()
    backend = service.read("backend")["content"]
    integration = service.read("integration")["content"]
    query = service.read("sql")["content"]
    debug = service.read("debug")["content"]

    assert "@api.readonly" in backend
    assert "models.Constraint" in backend
    assert "_commit_progress()" in integration
    assert "small deterministic" in integration
    assert "search_fetch()" in query
    assert "version label alone" in query
    assert "ON-DEMAND TRACE BUDGET" in debug
    assert "do not force diagrams" in debug
    assert service.list()["count"] == len(EXPECTED)
