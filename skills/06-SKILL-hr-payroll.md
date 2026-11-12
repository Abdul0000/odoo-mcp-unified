# IDENTITY
Treat the selected client's installed localization and payroll source as final
authority; verify model availability and current statutory rules before use.

Odoo HR/payroll engineer. Payslip computation must trace every line to a
`hr.salary.rule` — never hardcode net pay. Contract state gates payroll
eligibility; an employee without an active `hr.contract` cannot get a payslip.

Make effective-date, contract, localization, and statutory assumptions explicit;
trace material effects through payslip lines, employee balances, and accounting.

---

## 1. EMPLOYEE & CONTRACT
```python
class HrContract(models.Model):
    _inherit = 'hr.contract'

    employee_id = fields.Many2one('hr.employee', required=True)
    state = fields.Selection([
        ('draft', 'New'), ('open', 'Running'),
        ('close', 'Expired'), ('cancel', 'Cancelled'),
    ])
    wage = fields.Monetary(required=True)  # base gross salary
    structure_type_id = fields.Many2one('hr.payroll.structure.type')
    date_start = fields.Date(required=True)
    date_end = fields.Date()

# Only ONE active contract per employee at a time — check before creating:
active = self.env['hr.contract'].search([
    ('employee_id', '=', emp.id), ('state', '=', 'open')
])
```

## 2. SALARY STRUCTURE & RULES
```python
class HrSalaryRule(models.Model):
    _inherit = 'hr.salary.rule'

    code = fields.Char(required=True)          # e.g. 'BASIC', 'HRA', 'PF'
    category_id = fields.Many2one('hr.salary.rule.category')  # Allowance/Deduction
    amount_select = fields.Selection([
        ('percentage', 'Percentage'), ('fix', 'Fixed Amount'),
        ('code', 'Python Code'),
    ])
    amount_python_compute = fields.Text()
    # Example python rule for a deduction:
    #   result = -contract.wage * 0.05   (5% deduction)
```

## 3. PAYSLIP LIFECYCLE
```python
class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    name = fields.Char(default=lambda self: self.env['ir.sequence'].next_by_code(
        'salary.slip'
    ))
    state = fields.Selection([
        ('draft', 'Draft'), ('verify', 'Waiting'),
        ('done', 'Done'), ('paid', 'Paid'), ('cancel', 'Rejected'),
    ])
    line_ids = fields.One2many('hr.payslip.line', 'slip_id')

    def compute_sheet(self):
        # Runs every hr.salary.rule attached to the structure, in
        # sequence order — DO NOT manually compute net pay outside this.
        return super().compute_sheet()
```

## 4. BATCH PAYROLL RUN
```python
batch = self.env['hr.payslip.run'].create({
    'name': 'Salary Run - Jul 2026', 'date_start': '2026-07-01',
    'date_end': '2026-07-31',
})
wizard = self.env['hr.payslip.employees'].create({
    'employee_ids': [(6, 0, employees.ids)],
})
wizard.with_context(active_id=batch.id).compute_sheet()
```

## 5. LEAVE / ATTENDANCE INTEGRATION
```python
# hr.leave (approved) auto-generates a 'WORKED100'/'LEAVE' worked_days_line
# on the payslip via _get_worked_day_lines(); never subtract leave pay
# manually — it double-counts against the salary rule.
leave = self.env['hr.leave'].create({
    'employee_id': emp.id, 'holiday_status_id': leave_type.id,
    'date_from': start, 'date_to': end,
})
leave.action_approve()
```

## 6. DEDUCTIONS (statutory / loans)
```python
# Custom deduction rule pattern:
class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    def _get_deductions(self):
        for slip in self:
            loan = self.env['hr.employee.loan'].search([
                ('employee_id', '=', slip.employee_id.id), ('state', '=', 'running')
            ], limit=1)
            if loan:
                installment = min(loan.installment_amount, loan.balance)
                loan.balance -= installment
                # write installment as a payslip.line with category 'DED'
```

## 7. PAKISTAN STATUTORY PATTERN (example — verify current FBR slabs)
```python
# Income tax slabs change per Finance Act — never hardcode a slab table
# permanently. Structure it as a lookup so it's updated yearly:
tax_slabs = self.env['hr.tax.slab'].search([
    ('date_from', '<=', payslip.date_to), ('date_to', '>=', payslip.date_from)
], order='min_amount asc')
# Apply progressively per slab.min_amount/max_amount/rate — do not apply
# flat top rate to the whole salary.
```

## 8. ATTENDANCE → WORKED DAYS
```python
attendances = self.env['hr.attendance'].search([
    ('employee_id', '=', emp.id),
    ('check_in', '>=', period_start), ('check_out', '<=', period_end),
])
worked_hours = sum(
    (a.check_out - a.check_in).total_seconds() / 3600 for a in attendances if a.check_out
)
```

## 9. PAYMENT RUN → BANK FILE / GL
```python
# hr.payslip.run -> account.move (payroll journal entry) via
# payslip.action_payslip_done() -> creates move with salary rule GL mapping
# (hr.salary.rule.account_debit/account_credit)
```

## 10. PAYROLL ANALYSIS (SQL)
```sql
SELECT hp.employee_id, he.name, hp.date_from, hp.date_to,
       SUM(CASE WHEN c.code = 'GROSS' THEN hpl.total ELSE 0 END) AS gross,
       SUM(CASE WHEN c.type = 'deduction' THEN hpl.total ELSE 0 END) AS deductions,
       SUM(CASE WHEN hpl.code = 'NET' THEN hpl.total ELSE 0 END) AS net
FROM hr_payslip hp
JOIN hr_payslip_line hpl ON hpl.slip_id = hp.id
JOIN hr_salary_rule_category c ON c.id = hpl.category_id
JOIN hr_employee he ON he.id = hp.employee_id
WHERE hp.state IN ('done', 'paid')
GROUP BY hp.employee_id, he.name, hp.date_from, hp.date_to;
```

---

## CRITICAL RULES
- Never bypass `compute_sheet()` to hand-write payslip lines — it breaks the
  audit trail back to salary rules.
- An employee must have exactly one `open` contract; overlapping open
  contracts are a data integrity bug, not a feature.
- Statutory tax/deduction rates are time-bound — always scope by
  effective date range, never a single global constant.
- Implement via `workflow.odoo_code_change` (server-owned read/write/verify/deploy). Never call hidden repo/git/odoosh tools directly.
