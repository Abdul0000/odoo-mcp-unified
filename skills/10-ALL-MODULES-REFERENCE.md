# IDENTITY
Resolve module/model/XML-ID ownership from the cached source index and selected
installed source; this lookup must not guess availability from a static table.

Quick-lookup map for choosing the right Odoo app/model before implementation.
Not a coding skill — route to the specific module skill (odoo-accounting,
odoo-sales, etc.) once the right module is identified.

---

## CORE BUSINESS MODULES → SKILL TO USE NEXT

| Need | Odoo App | Key Model | Use Skill |
|---|---|---|---|
| Invoicing, GL, tax | Accounting | `account.move` | odoo-accounting |
| Production, BoM | Manufacturing | `mrp.production` | odoo-manufacturing |
| Stock, transfers | Inventory | `stock.move` | odoo-inventory |
| Quotes, orders | Sales | `sale.order` | odoo-sales |
| RFQ, vendor bills | Purchase | `purchase.order` | odoo-purchase |
| Payroll, contracts | HR/Payroll | `hr.payslip` | odoo-hr-payroll |
| Leads, pipeline | CRM | `crm.lead` | odoo-crm-pipeline |
| Support tickets | Helpdesk | `helpdesk.ticket` | odoo-helpdesk |
| Reporting/analytics | — | any model | odoo-sql-queries |

## OTHER COMMON MODULES (not covered by a dedicated skill — use general
## odoo-devlopement-version-16to19 skill + read source)

**POS & Retail**
- `point_of_sale` — `pos.order`, `pos.session`, `pos.config`
- `pos_restaurant` — table management, kitchen printers

**Website & eCommerce**
- `website_sale` — `product.template.website_published`, cart/checkout flow
- `website` — `website.page`, builder snippets

**Project & Timesheets**
- `project` — `project.task`, kanban stages, `project.milestone`
- `hr_timesheet` — `account.analytic.line`, links to billable sale order lines

**Field Service**
- `industry_fsm` — `project.task` extended with `fsm_done`, worksheet templates

**Documents & Sign**
- `documents` — `documents.document`, workspace/tag based DMS
- `sign` — `sign.request`, `sign.template`

**Marketing**
- `marketing_automation` — `marketing.campaign`, activity-based triggers
- `mass_mailing` — `mailing.mailing`, A/B testing, link tracking
- `event` — `event.event`, `event.registration`

**Studio & Low-Code**
- `web_studio` — customizes views/fields without code (Enterprise only)

**Multi-company / Localization**
- `l10n_pk` — Pakistan chart of accounts, tax templates
- Always check `l10n_<country>` module exists before building custom
  fiscal localization from scratch

## MODULE DEPENDENCY QUICK-CHECK
```python
# Before building a custom module, check what's already installed/available:
installed = self.env['ir.module.module'].search([('state', '=', 'installed')])
# Check a specific module's dependency chain:
module = self.env['ir.module.module'].search([('name', '=', 'sale')])
module.dependencies_id.mapped('name')
```

## CROSS-MODULE INTEGRATION POINTS (common gotchas)
- **Sale → Inventory → Accounting**: `sale.order` confirm creates
  `stock.picking`; delivery validation feeds `qty_delivered`; invoicing
  creates `account.move`. Break in this chain = check `invoice_policy` first.
- **Purchase → Inventory → Accounting**: mirror of above, `qty_received`
  drives vendor bill matching.
- **Manufacturing → Inventory**: `mrp.production` consumes/produces via
  `stock.move` — component shortage blocks `action_confirm` reservation,
  not the MO creation itself.
- **HR → Payroll**: `hr.contract` must be `state='open'` for `hr.payslip`
  computation to find a valid salary structure.
- **CRM → Sales**: `crm.lead.convert_opportunity()` doesn't auto-create a
  `sale.order` — that's a separate manual/automated step.

## IMPLEMENTATION ROADMAP PATTERN (typical SME rollout order)
1. Contacts + Company setup (`res.partner`, `res.company`, chart of accounts)
2. Sales + Inventory (core revenue flow)
3. Purchase (procurement flow)
4. Accounting (close the financial loop)
5. HR/Payroll (if in scope)
6. CRM (if sales team needs pipeline before order creation)
7. Manufacturing (only if the business actually produces goods)
8. Helpdesk/Project (post-sale service layer)

---

## CRITICAL RULE
This is a routing reference, not implementation guidance — once the module
is identified, switch to its dedicated skill (or read actual source under
`addons/<module>/models/`) before writing code.
