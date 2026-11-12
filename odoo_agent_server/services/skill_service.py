from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status


_SKILLS = {
    "odoo-accounting-development": ("01-SKILL-accounting.md", "Accounting, finance, invoices, journals, payments, taxes, reconciliation, and reporting."),
    "odoo-manufacturing-development": ("02-SKILL-manufacturing.md", "MRP, BoMs, manufacturing/work orders, quality, subcontracting, planning, and costing."),
    "odoo-inventory-development": ("03-SKILL-inventory.md", "Inventory, warehouses, locations, pickings, moves, reservations, tracking, routes, and valuation."),
    "odoo-sales-development": ("04-SKILL-sales.md", "Sales orders, quotations, pricing, discounts, delivery, invoicing, subscriptions, portal, and credit controls."),
    "odoo-purchase-development": ("05-SKILL-purchase.md", "Purchase orders, vendors, agreements, approvals, replenishment, receipts, billing, landed costs, and procurement analytics."),
    "odoo-hr-payroll-development": ("06-SKILL-hr-payroll.md", "HR, contracts, payroll, payslips, attendance, leave, timesheets, and statutory deductions."),
    "odoo-crm-pipeline-development": ("07-SKILL-crm-pipeline.md", "CRM leads/opportunities, stages, teams, scoring, activities, forecasting, automation, and pipeline reporting."),
    "odoo-helpdesk-development": ("08-SKILL-helpdesk.md", "Helpdesk tickets, teams, SLAs, escalation, communication, portal, knowledge, automation, and analytics."),
    "odoo-sql-query-development": ("09-SKILL-sql-queries.md", "Odoo ORM and SQL queries, domains, joins, read_group, analytics, reporting, and query performance."),
    "odoo-all-modules-reference": ("10-ALL-MODULES-REFERENCE.md", "Odoo module selection, dependencies, key models, integration planning, and implementation roadmap."),
    "odoo-qweb-reports-development": ("11-SKILL-qweb-reports-development.md", "General QWeb PDF/HTML reports, templates, controllers, styling, pagination, data fetching, performance, and debugging."),
    "odoo-accounting-reports": ("12-SKILL-accounting-reports.md", "Accounting QWeb reports including invoices, AR aging, P&L, balance sheet, tax reports, statements, and Pakistan compliance reporting."),
    "odoo-pos-development": ("odoo-pos-development-skill.md", "POS sessions, orders, payments, OWL UI, receipts, hardware, restaurant, loyalty, and reporting."),
    "odoo-website-development": ("odoo-website-development.md", "Website themes, QWeb, snippets, eCommerce, controllers, multi-site, SEO, and performance."),
    "odoo-custom-module-development": ("13-SKILL-custom-module.md", "Standalone custom modules with their own models/tables, manifest, security, views, and menus — not inherited/mixed into native Odoo models."),
    "odoo-task-planning": ("14-SKILL-planning.md", "Pre-implementation planning: scope classification (surgical vs new feature vs new module), file inventory checklist, impact/dependency check, and risk flags before writing code."),
    "odoo-view-development": ("15-SKILL-views.md", "Version-aware backend views, XML/QWeb, XPath, dependencies, inheritance priorities, widgets, security visibility, noupdate, Studio overlays, actions, and menus."),
    "odoo-backend-development": ("16-SKILL-backend.md", "Inherited Odoo Python models, ORM methods, fields, computes, constraints, workflows, cron logic, batching, and chatter behavior."),
    "odoo-frontend-owl-development": ("17-SKILL-frontend-owl.md", "Odoo web-client JavaScript, OWL components, services, hooks, registries, patches, client actions, widgets, templates, and assets."),
    "odoo-integration-development": ("18-SKILL-integration.md", "External APIs, webhooks, provider clients, mappings, queues, retries, idempotency, rate limits, and integration audit state."),
    "odoo-security-development": ("19-SKILL-security.md", "ACLs, groups, record rules, multi-company isolation, controller authorization, portal tokens, field visibility, and scoped sudo."),
    "odoo-runtime-debugging": ("20-SKILL-runtime-debugging.md", "Evidence-bounded Odoo runtime diagnosis for tracebacks, repeated failures, controller/cron/queue/API errors, browser defects, access issues, and performance failures."),
    "odoo-test-development": ("21-SKILL-tests.md", "Focused Odoo regression tests for models, security, controllers, integrations, reports, migrations, queues, OWL, and browser flows."),
    "odoo-code-review": ("22-SKILL-code-review.md", "Odoo module/PR/diff review for concrete bugs, security leaks, regressions, performance, upgrade risk, missing tests, and evidence-backed completion."),
    "odoo-upgrade-migration": ("23-SKILL-upgrade-migration.md", "Odoo source-to-target compatibility, deprecated APIs, view/OWL/assets changes, manifest changes, noupdate behavior, and data migrations."),
    "odoo-ai-mcp-development": ("24-SKILL-ai-mcp.md", "Odoo-connected MCP tools, AI assistants, LLM providers, RAG, permission-aware actions, structured outputs, audit, and truthful completion."),
    "odoo-functional-flow-testing": ("25-SKILL-functional-flow-testing.md", "Live permission-aware Odoo record mutations and verified Sales, Purchase, Inventory, MRP, and Accounting flow tests."),
}

_ALIASES = {
    "accounting": "odoo-accounting-development",
    "manufacturing": "odoo-manufacturing-development",
    "mrp": "odoo-manufacturing-development",
    "inventory": "odoo-inventory-development",
    "stock": "odoo-inventory-development",
    "sales": "odoo-sales-development",
    "purchase": "odoo-purchase-development",
    "procurement": "odoo-purchase-development",
    "hr-payroll": "odoo-hr-payroll-development",
    "payroll": "odoo-hr-payroll-development",
    "crm": "odoo-crm-pipeline-development",
    "helpdesk": "odoo-helpdesk-development",
    "sql": "odoo-sql-query-development",
    "sql-queries": "odoo-sql-query-development",
    "query": "odoo-sql-query-development",
    "queries": "odoo-sql-query-development",
    "orm": "odoo-sql-query-development",
    "database": "odoo-sql-query-development",
    "analytics": "odoo-sql-query-development",
    "report": "odoo-qweb-reports-development",
    "reports": "odoo-qweb-reports-development",
    "reporting": "odoo-sql-query-development",
    "pos": "odoo-pos-development",
    "website": "odoo-website-development",
    "modules": "odoo-all-modules-reference",
    "all-modules": "odoo-all-modules-reference",
    "module-reference": "odoo-all-modules-reference",
    "qweb": "odoo-qweb-reports-development",
    "qweb-report": "odoo-qweb-reports-development",
    "qweb-reports": "odoo-qweb-reports-development",
    "qweb-reporting": "odoo-qweb-reports-development",
    "pdf-report": "odoo-qweb-reports-development",
    "pdf-reports": "odoo-qweb-reports-development",
    "report-template": "odoo-qweb-reports-development",
    "accounting-report": "odoo-accounting-reports",
    "accounting-reports": "odoo-accounting-reports",
    "financial-report": "odoo-accounting-reports",
    "financial-reports": "odoo-accounting-reports",
    "invoice-report": "odoo-accounting-reports",
    "ar-aging": "odoo-accounting-reports",
    "profit-loss": "odoo-accounting-reports",
    "balance-sheet": "odoo-accounting-reports",
    "tax-report": "odoo-accounting-reports",
    "custom-module": "odoo-custom-module-development",
    "custom module": "odoo-custom-module-development",
    "new-module": "odoo-custom-module-development",
    "standalone-module": "odoo-custom-module-development",
    "own-model": "odoo-custom-module-development",
    "scratch-module": "odoo-custom-module-development",
    "planning": "odoo-task-planning",
    "plan": "odoo-task-planning",
    "task-plan": "odoo-task-planning",
    "scope": "odoo-task-planning",
    "checklist": "odoo-task-planning",
    "view": "odoo-view-development",
    "views": "odoo-view-development",
    "xpath": "odoo-view-development",
    "xml-view": "odoo-view-development",
    "backend-view": "odoo-view-development",
    "view-xml": "odoo-view-development",
    "backend": "odoo-backend-development",
    "python-model": "odoo-backend-development",
    "orm-model": "odoo-backend-development",
    "model-inheritance": "odoo-backend-development",
    "owl": "odoo-frontend-owl-development",
    "frontend": "odoo-frontend-owl-development",
    "javascript": "odoo-frontend-owl-development",
    "integration": "odoo-integration-development",
    "api-integration": "odoo-integration-development",
    "webhook": "odoo-integration-development",
    "security": "odoo-security-development",
    "access-rights": "odoo-security-development",
    "record-rules": "odoo-security-development",
    "debug": "odoo-runtime-debugging",
    "debugging": "odoo-runtime-debugging",
    "runtime-error": "odoo-runtime-debugging",
    "tests": "odoo-test-development",
    "testing": "odoo-test-development",
    "regression-test": "odoo-test-development",
    "review": "odoo-code-review",
    "code-review": "odoo-code-review",
    "migration": "odoo-upgrade-migration",
    "version-upgrade": "odoo-upgrade-migration",
    "ai": "odoo-ai-mcp-development",
    "mcp": "odoo-ai-mcp-development",
    "rag": "odoo-ai-mcp-development",
    "functional-flow": "odoo-functional-flow-testing",
    "flow-test": "odoo-functional-flow-testing",
    "live-records": "odoo-functional-flow-testing",
}

_ROUTING_TERMS = {
    "odoo-view-development": (
        "xpath", "inherited view", "inherit_id", "form view", "list view",
        "tree view", "backend view", "xml view", "ir.ui.view", "studio view",
        "field placement", "add field to form", "button in view",
        "attrs states", "view priority",
    ),
    "odoo-accounting-reports": (
        "accounting report", "financial report", "invoice report", "ar aging",
        "aged receivable", "profit loss", "balance sheet", "tax report",
        "partner statement", "account report filter", "account_reports",
        "accountreportfilters", "updateoption", "report options",
    ),
    "odoo-runtime-debugging": (
        "traceback", "runtime error", "server error", "javascript error",
        "console error", "cron failed", "queue failed", "webhook failed",
        "same issue persists", "still not fixed", "repeated failure",
    ),
    "odoo-test-development": (
        "regression test", "savepointcase", "transactioncase", "httptest",
        "httpcase", "mocked test", "test case", "test coverage",
    ),
    "odoo-code-review": (
        "code review", "review this diff", "review this module", "audit this module",
        "security review", "production readiness", "pull request review",
    ),
    "odoo-upgrade-migration": (
        "version migration", "migrate from", "migrate to", "upgrade compatibility",
        "deprecated api", "migration script", "pre-migrate", "post-migrate",
    ),
    "odoo-ai-mcp-development": (
        "mcp tool", "mcp server", "model context protocol", "rag pipeline",
        "llm provider", "openai integration", "ai assistant", "embedding",
    ),
    "odoo-functional-flow-testing": (
        "flow test", "functional flow", "run sale order", "run purchase order",
        "create live record", "update live record", "record action",
        "record_create", "record_write", "persistent_test",
    ),
    "odoo-qweb-reports-development": ("qweb", "pdf report", "print report", "report template", "paperformat", "wkhtmltopdf", "ir.actions.report"),
    "odoo-sql-query-development": ("sql", "query", "search_read", "read_group", "domain", "group by", "aggregate", "record count", "analytics"),
    "odoo-frontend-owl-development": (
        "owl component", "client action", "field widget", "use service",
        "useservice", "registry category", "javascript patch", "web.assets_backend",
    ),
    "odoo-integration-development": (
        "external api", "api integration", "webhook", "provider client",
        "rate limit", "external id", "sync queue", "third party",
    ),
    "odoo-security-development": (
        "access control", "access rights", "ir.model.access", "record rule",
        "ir.rule", "cross company", "portal token", "permission denied",
    ),
    "odoo-backend-development": (
        "computed field", "onchange", "python constraint", "model constraint",
        "override create", "override write", "model inheritance", "_inherit",
        "message_post", "mail.thread", "batch method",
    ),
    "odoo-pos-development": ("point of sale", "pos ", "pos_", "receipt", "payment screen", "restaurant pos"),
    "odoo-website-development": ("website", "web page", "landing page", "snippet", "ecommerce", "portal page", "seo"),
    "odoo-manufacturing-development": ("manufacturing", "mrp", "bill of materials", "bom", "work order"),
    "odoo-inventory-development": ("inventory", "warehouse", "stock move", "picking", "location", "reservation"),
    "odoo-sales-development": ("sale order", "sales order", "quotation", "pricelist", "subscription"),
    "odoo-purchase-development": ("purchase order", "vendor", "procurement", "replenishment", "request for quotation", "rfq"),
    "odoo-hr-payroll-development": ("payroll", "payslip", "employee", "attendance", "leave", "timesheet", "contract"),
    "odoo-crm-pipeline-development": ("crm", "lead", "opportunity", "pipeline", "sales team"),
    "odoo-helpdesk-development": ("helpdesk", "ticket", "sla", "support team", "escalation"),
    "odoo-accounting-development": ("invoice", "journal", "payment", "tax", "reconciliation", "fiscal position", "account.move"),
    "odoo-all-modules-reference": ("which module", "module dependency", "depends", "module selection", "implementation roadmap"),
    "odoo-custom-module-development": ("custom module", "new module", "standalone module", "own model", "own table", "scratch module", "_name =", "not inherit"),
    "odoo-task-planning": ("plan", "planning", "scope this", "before implementing", "file checklist", "what files are needed", "break down the task"),
}


def _validate_registry() -> None:
    filenames = [filename for filename, _description in _SKILLS.values()]
    if len(filenames) != len(set(filenames)):
        raise RuntimeError("Each authoritative Odoo skill must own one unique source file.")
    unknown_aliases = sorted(set(_ALIASES.values()) - set(_SKILLS))
    unknown_routes = sorted(set(_ROUTING_TERMS) - set(_SKILLS))
    if unknown_aliases or unknown_routes:
        raise RuntimeError(
            f"Invalid skill registry references: aliases={unknown_aliases}, routes={unknown_routes}"
        )


_validate_registry()


def _skills_root() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


class SkillService:
    @staticmethod
    def names() -> list[str]:
        return list(_SKILLS)

    def list(self, task_description: str | None = None) -> dict:
        result = {
            "skills": [
                {"name": name, "description": description}
                for name, (_, description) in _SKILLS.items()
            ],
            "count": len(_SKILLS),
            "loading": "on_demand",
            "selection_policy": "single_primary_owner",
        }
        if str(task_description or "").strip():
            result["recommendation"] = self.recommend(task_description or "")
        return result

    @staticmethod
    def recommend(task_description: str) -> dict | None:
        text = f" {str(task_description or '').lower()} "
        ranked: list[tuple[int, int, str, list[str]]] = []
        for priority, (name, terms) in enumerate(_ROUTING_TERMS.items()):
            matched = [term for term in terms if term in text]
            if matched:
                score = sum(3 if " " in term or "." in term or "_" in term else 1 for term in matched)
                ranked.append((score, -priority, name, matched))
        if not ranked:
            return None
        score, _, name, matched = max(ranked)
        return {
            "name": name,
            "matched_terms": matched[:5],
            "confidence": "high" if score >= 3 else "medium",
            "selection_policy": "single_primary_owner",
        }

    def read(self, name: str) -> dict:
        requested = str(name or "").strip().lower()
        canonical = _ALIASES.get(requested, requested)
        entry = _SKILLS.get(canonical)
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown skill '{name}'. Call skills.list for available names.",
            )
        filename, description = entry
        path = (_skills_root() / filename).resolve()
        root = _skills_root().resolve()
        if root not in path.parents or not path.is_file():
            raise HTTPException(status_code=500, detail=f"Skill resource missing: {canonical}")
        return {
            "name": canonical,
            "description": description,
            "content": path.read_text(encoding="utf-8"),
            "source": f"skills/{filename}",
            "exact": True,
        }
