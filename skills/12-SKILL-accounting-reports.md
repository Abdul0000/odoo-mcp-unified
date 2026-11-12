# IDENTITY
Treat the selected client's installed `account_reports`, accounting, and
localization source as final authority; examples are not cross-version APIs.

Odoo financial report engineer. Every total must trace to `account.move.line`
debit/credit, never a hardcoded number. Tax/withholding rates are looked up
per-tax-record — never hardcoded percentages, since Pakistan FBR rates
change by Finance Act.

Apply the `odoo-view-development` QWeb contract to invoice/financial templates:
resolve transitive layouts and calls, dependencies, XPath targets, safe output,
assets, company/language context, and final rendered proof.

Identify the end-to-end report crux and keep normalized options, rows, totals,
labels, currencies, companies, and every export surface reconciled.

---

## NATIVE `account_reports` CLIENT CONTRACT

Interactive financial reports are OWL client actions, not ordinary QWeb/PDF
reports. Before changing a filter, read the exact installed-version
`AccountReport`, `AccountReportFilters`, action loader, option initializer, and
one native filter implementation. Treat their signatures as the contract.

For Odoo 18, verify the installed source still matches this action shape:

```xml
<field name="tag">account_report</field>
<field name="params"
       eval="{
           'report_id': ref('module_name.report_xmlid'),
           'options': {},
           'ignore_session': True
       }"/>
```

- Put report startup data in `ir.actions.client.params`, not `context`, when
  the installed action loader reads `params`.
- Pass `options` as a mutable mapping (`{}`), never JSON text (`"{}"`).
  Native code mutates the object during startup.
- Directly depend on every addon whose models/XML IDs/templates are referenced;
  an `account_reports` dependency does not replace a direct `account`
  dependency.
- Do not call `controller.reload()` from a filter unless the installed method
  contract supplies its required action. In the Odoo 18 contract, apply an
  option through the native path:

```javascript
await this.controller.updateOption("top_x", topX, true);
```

The first argument is an option-path string, not a replacement options object;
the apply flag uses the native refresh path while preserving report/action
state. Reconfirm this signature for the selected version instead of copying it
across releases.

## OPTION LIFECYCLE AND CONSISTENCY

- Define one canonical option key and trace it end to end: action defaults →
  session/current options → backend initializer → query/domain → displayed
  rows → total label.
- Let an explicit current request override persisted/previous options:
  `options.get(key, previous_options.get(key, default))`, unless installed core
  proves another precedence.
- Parse and normalize once. For Top N, reject booleans/non-numeric values,
  default deterministically, clamp to at least 1, and use the normalized value
  for slicing, totals, labels, and persisted options.
- Count business rows only; do not include a synthetic total row in Top N.
- Generate date labels from normalized report date options with locale/company
  context. Verify single-month and multi-month ranges, previous/current column
  labels, and the date-button label from the same boundaries.
- Avoid duplicate apply events. A safe Enter pattern is to prevent default and
  blur the input; let the single `change` handler apply the option once:

```javascript
onTopXKeydown(event) {
    if (event.key === "Enter") {
        event.preventDefault();
        event.target.blur();
    }
}
```

## REPORT ENGINE AND EXPORT CONTRACT

- Resolve the report model, custom handler, engine/line generator, expressions,
  columns, and frontend component from the exact installed source.
- Trace every expression label to the backend result key that supplies it.
- Keep row IDs, unfold IDs, load-more IDs, column groups, comparison periods,
  and synthetic total rows stable across refreshes.
- Reconcile opening balance + current movement = closing balance where the
  report semantics require it.
- Apply the same normalized options, companies, currencies, date scope, and
  access context to interactive rows, totals, PDF, and spreadsheet exports.
- Use the installed currency-table/accounting helpers and parameterized SQL;
  never omit company/security scope from a raw reporting query.

## INTERACTIVE REPORT TERMINAL PROOF

Do not stop at “no console error.” Prove all of the following on the deployed
bundle and representative accounting user:

- action opens without `sessionOptions`/JSON startup errors;
- changing the filter issues one native refresh;
- backend receives the normalized current option;
- row count, total-row label, date labels, and exported/printed result agree;
- reopening the report follows the intended session persistence policy;
- browser asset URL/hash and loaded module contain the deployed implementation.

---

## 1. INVOICE REPORT (multi-tax safe)
```xml
<record id="action_report_invoice_custom" model="ir.actions.report">
  <field name="name">Invoice</field>
  <field name="model">account.move</field>
  <field name="report_type">qweb-pdf</field>
  <field name="report_name">account_custom.report_invoice</field>
  <field name="binding_model_id" ref="account.model_account_move"/>
  <field name="binding_type">report</field>
</record>

<template id="report_invoice">
  <t t-call="web.html_container">
    <t t-call="web.external_layout">
      <t t-foreach="docs" t-as="doc">
        <div class="page">
          <h2>INVOICE <t t-esc="doc.name"/></h2>
          <p><strong>Bill To:</strong> <t t-esc="doc.partner_id.name"/></p>
          <p t-if="doc.partner_id.vat"><strong>Tax ID:</strong> <t t-esc="doc.partner_id.vat"/></p>

          <table class="table table-sm">
            <thead>
              <tr><th>Description</th><th class="text-right">Qty</th>
                  <th class="text-right">Unit Price</th><th class="text-right">Amount</th></tr>
            </thead>
            <tbody>
              <t t-foreach="doc.invoice_line_ids" t-as="line">
                <tr>
                  <td t-esc="line.product_id.name or line.name"/>
                  <td class="text-right" t-esc="line.quantity"/>
                  <td class="text-right" t-esc="'{:.2f}'.format(line.price_unit)"/>
                  <td class="text-right" t-esc="'{:.2f}'.format(line.price_subtotal)"/>
                </tr>
              </t>
            </tbody>
          </table>

          <!-- Tax breakdown: group by tax record across ALL lines, not just line[0] -->
          <table class="table table-sm">
            <tr><td><strong>Subtotal:</strong></td>
                <td class="text-right" t-esc="'{:.2f}'.format(doc.amount_untaxed)"/></tr>
            <t t-foreach="doc.line_ids.filtered(lambda l: l.tax_line_id).mapped('tax_line_id')" t-as="tax">
              <tr>
                <td><strong t-esc="tax.name"/>:</td>
                <td class="text-right">
                  <t t-set="tax_amount" t-value="sum(
                      doc.line_ids.filtered(lambda l: l.tax_line_id == tax).mapped('balance')
                  )"/>
                  <t t-esc="'{:.2f}'.format(abs(tax_amount))"/>
                </td>
              </tr>
            </t>
            <tr class="border-top border-dark">
              <td><strong>Total:</strong></td>
              <td class="text-right"><strong t-esc="'{:.2f}'.format(doc.amount_total)"/></td>
            </tr>
          </table>
        </div>
        <div t-if="not doc_last" class="page-break"/>
      </t>
    </t>
  </t>
</template>
```
**Fix note**: tax totals must be read from `account.move.line` records
where `tax_line_id` is set (the actual posted tax lines) — grouping by
`invoice_line_ids[0].tax_ids` breaks the moment different lines carry
different taxes. Always sum from the real tax lines, not line-zero's config.

## 2. AGED RECEIVABLE
```python
def get_aged_receivable(self, as_of_date=None):
    as_of_date = as_of_date or fields.Date.today()
    self.env.cr.execute("""
        SELECT p.id AS partner_id, p.name AS partner_name, am.name AS invoice_number,
               am.date AS invoice_date, am.amount_total, am.amount_residual,
               CASE WHEN %(today)s - am.date <= 30 THEN am.amount_residual ELSE 0 END AS b_1_30,
               CASE WHEN %(today)s - am.date BETWEEN 31 AND 60 THEN am.amount_residual ELSE 0 END AS b_31_60,
               CASE WHEN %(today)s - am.date BETWEEN 61 AND 90 THEN am.amount_residual ELSE 0 END AS b_61_90,
               CASE WHEN %(today)s - am.date > 90 THEN am.amount_residual ELSE 0 END AS b_90_plus
        FROM account_move am
        JOIN res_partner p ON am.partner_id = p.id
        WHERE am.move_type = 'out_invoice' AND am.state = 'posted'
          AND am.amount_residual > 0.01
        ORDER BY p.name, am.date
    """, {'today': as_of_date})
    return self.env.cr.dictfetchall()
```
Same pattern for AP aging: swap `move_type = 'out_invoice'` for `'in_invoice'`.

## 3. PROFIT & LOSS (real implementation, not a stub)
```python
def get_pl_section(self, section, date_from, date_to):
    """section: 'revenue' | 'cogs' | 'expenses' — maps to account.account.internal_group"""
    group_map = {'revenue': 'income', 'cogs': 'expense', 'expenses': 'expense'}
    self.env.cr.execute("""
        SELECT aa.name AS account_name, SUM(aml.balance) * -1 AS amount
        FROM account_move_line aml
        JOIN account_account aa ON aa.id = aml.account_id
        JOIN account_move am ON am.id = aml.move_id
        WHERE aa.internal_group = %s AND am.state = 'posted'
          AND am.date BETWEEN %s AND %s
        GROUP BY aa.id, aa.name ORDER BY aa.name
    """, (group_map[section], date_from, date_to))
    return self.env.cr.dictfetchall()

def get_pl_total(self, section, date_from, date_to):
    return sum(r['amount'] for r in self.get_pl_section(section, date_from, date_to))
```
`internal_group` distinguishes income/expense reliably across chart of
accounts localizations — don't filter by account code prefix, it varies
per `l10n_<country>` template.

## 4. BALANCE SHEET (real implementation)
```python
def get_balance_sheet_accounts(self, acc_type, as_of_date):
    """acc_type: 'asset' | 'liability' | 'equity' — internal_group"""
    self.env.cr.execute("""
        SELECT aa.name AS account_name, SUM(aml.balance) AS balance
        FROM account_move_line aml
        JOIN account_account aa ON aa.id = aml.account_id
        JOIN account_move am ON am.id = aml.move_id
        WHERE aa.internal_group = %s AND am.state = 'posted' AND am.date <= %s
        GROUP BY aa.id, aa.name
        HAVING SUM(aml.balance) != 0
        ORDER BY aa.name
    """, (acc_type, as_of_date))
    return self.env.cr.dictfetchall()

def get_balance_sheet_total(self, acc_type, as_of_date):
    return sum(r['balance'] for r in self.get_balance_sheet_accounts(acc_type, as_of_date))
```
Assets should net positive (debit-heavy); liabilities/equity net negative
in raw `balance` — flip sign in the template if presenting as positive
figures, but keep the underlying query sign-consistent with the GL.

## 5. TAX COMPLIANCE REPORT (Pakistan GST + Withholding — rate lookup, not hardcoded)
```python
def get_tax_summary(self, tax_type, date_from, date_to):
    """tax_type: filter by account.tax.tax_group_id or name pattern, not a hardcoded rate"""
    self.env.cr.execute("""
        SELECT at.name AS tax_name, at.amount AS rate,
               SUM(aml.balance) * -1 AS tax_amount,
               SUM(rel.debit + rel.credit) AS base_amount
        FROM account_move_line aml
        JOIN account_tax at ON at.id = aml.tax_line_id
        JOIN account_move am ON am.id = aml.move_id
        LEFT JOIN account_move_line rel ON rel.id = ANY(
            SELECT unnest(regexp_split_to_array(aml.tax_base_amount::text, ','))::int
        )
        WHERE am.state = 'posted' AND am.date BETWEEN %s AND %s
          AND at.type_tax_use = %s
        GROUP BY at.id, at.name, at.amount
    """, (date_from, date_to, 'sale' if tax_type == 'sales_tax' else 'purchase'))
    return self.env.cr.dictfetchall()

def get_withholding_tax(self, date_from, date_to):
    """Withholding modeled as a negative-rate purchase tax, or a dedicated
    account.tax with tax_group_id = 'withholding' — adjust filter to match
    your l10n_pk / custom WHT tax group setup."""
    self.env.cr.execute("""
        SELECT p.name AS vendor_name, am.amount_total AS invoice_amount,
               at.amount AS rate, SUM(aml.balance) * -1 AS wht_amount
        FROM account_move_line aml
        JOIN account_tax at ON at.id = aml.tax_line_id
        JOIN account_move am ON am.id = aml.move_id
        JOIN res_partner p ON p.id = am.partner_id
        JOIN account_tax_group atg ON atg.id = at.tax_group_id
        WHERE am.state = 'posted' AND am.move_type = 'in_invoice'
          AND am.date BETWEEN %s AND %s AND atg.name ILIKE %s
        GROUP BY p.name, am.amount_total, at.amount
    """, (date_from, date_to, '%withhold%'))
    return self.env.cr.dictfetchall()
```
**Note**: the exact WHT tax-group name depends on how `l10n_pk` or your
custom tax setup names withholding taxes — verify the actual `tax_group_id`
name in the target database before relying on the `ILIKE` filter.

## 6. VENDOR/CUSTOMER PAYMENT STATEMENT
```python
def get_partner_statement(self, partner_id, date_from, date_to):
    self.env.cr.execute("""
        SELECT am.date, am.name AS invoice_number, am.amount_total,
               am.amount_total - am.amount_residual AS paid_amount,
               am.amount_residual AS balance
        FROM account_move am
        WHERE am.partner_id = %s AND am.state = 'posted'
          AND am.date BETWEEN %s AND %s
          AND am.move_type IN ('out_invoice', 'in_invoice')
        ORDER BY am.date
    """, (partner_id, date_from, date_to))
    return self.env.cr.dictfetchall()
```

---

## QUICK REFERENCE

| Report | Method | Key filter |
|---|---|---|
| Invoice | `doc.invoice_line_ids` + `line_ids.tax_line_id` | posted, per-line taxes |
| AR/AP Aging | `get_aged_receivable()` | `amount_residual > 0`, move_type |
| P&L | `get_pl_section()` | `account.internal_group` |
| Balance Sheet | `get_balance_sheet_accounts()` | `internal_group`, `date <=` |
| Tax Report | `get_tax_summary()` | `tax_line_id`, `type_tax_use` |
| Statement | `get_partner_statement()` | `partner_id`, `move_type` |

---

## CRITICAL RULES
- Never group tax totals by `line_ids[0].tax_ids` — always sum from actual
  posted `account.move.line` records where `tax_line_id` is set.
- Filter accounts by `internal_group` (income/expense/asset/liability/equity),
  not by account code prefix — code prefixes vary per localization template.
- Withholding/GST rates come from `account.tax` records active on the report
  date — never hardcode a percentage, Pakistan FBR rates change yearly.
- All amounts must reconcile to `account.move.line.balance` — a report
  number that can't be traced back to a GL line is a bug, not a feature.
