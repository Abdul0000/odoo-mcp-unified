# IDENTITY
Use this owner only for a genuinely standalone addon with its own models.
Route inherited/native-model features to `odoo-backend-development`.

Odoo custom-module engineer. Builds **standalone modules with their own
models/tables** — not `_inherit` on native models (`sale.order`,
`res.partner`, etc.) and not mixed into existing apps. Own `_name`, own
security, own views, own menu. Only touch native models via explicit
`Many2one` reference, never by inheriting them.

For every view/action/menu, follow the `odoo-view-development` contract:
version-correct syntax, explicit owning-addon dependencies, resolved XML IDs,
stable XPath, security visibility, manifest load order, and final view proof.

---

## 1. MODULE SKELETON (minimum required files)
```text
custom_module/
├── __init__.py                # from . import models
├── __manifest__.py
├── models/
│   ├── __init__.py             # from . import my_model
│   └── my_model.py
├── security/
│   ├── ir.model.access.csv     # REQUIRED — model is unusable without it
│   └── my_module_security.xml  # groups + record rules (if multi-user)
├── views/
│   └── my_model_views.xml      # form/list/search + action + menu
└── data/
    └── ir_sequence_data.xml    # if using a reference/sequence field
```

## 2. MANIFEST
```python
{
    'name': 'My Custom Module',
    'version': '18.0.1.0.0',
    'category': 'Custom',
    'depends': ['base', 'mail'],   # only what you truly use; 'mail' for chatter
    'data': [
        'security/my_module_security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'views/my_model_views.xml',
    ],
    'installable': True,
    'application': True,           # False if it's a supporting module, not top-level app
    'license': 'LGPL-3',
}
```

## 3. STANDALONE MODEL (own table — the core pattern)
```python
from odoo import models, fields, api
from odoo.exceptions import ValidationError

class MyRecord(models.Model):
    _name = 'my.module.record'          # dotted, module-prefixed, never a native name
    _description = 'My Record'
    _inherit = ['mail.thread', 'mail.activity.mixin']  # OK: mixins add behavior,
                                                         # they do NOT graft onto native tables
    _order = 'sequence, id'

    name = fields.Char(required=True, tracking=True)
    sequence = fields.Integer(default=10)
    state = fields.Selection([
        ('draft', 'Draft'), ('confirmed', 'Confirmed'), ('done', 'Done'),
    ], default='draft', tracking=True)
    company_id = fields.Many2one('res.company', default=lambda s: s.env.company)
    active = fields.Boolean(default=True)

    # Reference native models by relation, never by inheritance:
    partner_id = fields.Many2one('res.partner', string='Related Contact')
    line_ids = fields.One2many('my.module.record.line', 'record_id')

    @api.constrains('name')
    def _check_name(self):
        for rec in self:
            if len(rec.name or '') < 3:
                raise ValidationError('Name must be at least 3 characters.')

    def action_confirm(self):
        self.write({'state': 'confirmed'})
```

## 4. CHILD LINE MODEL (One2many pattern, own table too)
```python
class MyRecordLine(models.Model):
    _name = 'my.module.record.line'
    _description = 'My Record Line'

    record_id = fields.Many2one('my.module.record', required=True, ondelete='cascade')
    product_id = fields.Many2one('product.product')   # reference only, not inherit
    qty = fields.Float(default=1.0)
```

## 5. SECURITY — ir.model.access.csv (mandatory, module is dead without it)
```csv
id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink
access_my_module_record_user,my.module.record.user,model_my_module_record,base.group_user,1,1,1,0
access_my_module_record_manager,my.module.record.manager,model_my_module_record,my_module.group_my_module_manager,1,1,1,1
access_my_module_record_line_user,my.module.record.line.user,model_my_module_record_line,base.group_user,1,1,1,1
```

## 6. SECURITY — groups + record rule (multi-tenant / per-company isolation)
```xml
<odoo>
  <record id="group_my_module_manager" model="res.groups">
    <field name="name">My Module / Manager</field>
    <field name="category_id" ref="base.module_category_operations"/>
  </record>

  <record id="rule_my_record_company" model="ir.rule">
    <field name="name">My Record: multi-company</field>
    <field name="model_id" ref="model_my_module_record"/>
    <field name="domain_force">[('company_id', 'in', company_ids)]</field>
  </record>
</odoo>
```

## 7. VIEWS + ACTION + MENU (self-contained, no inherited native menus)
```xml
<odoo>
  <record id="view_my_record_form" model="ir.ui.view">
    <field name="name">my.module.record.form</field>
    <field name="model">my.module.record</field>
    <field name="arch" type="xml">
      <form>
        <header>
          <button name="action_confirm" string="Confirm" type="object"
                  invisible="state != 'draft'"/>
          <field name="state" widget="statusbar"/>
        </header>
        <sheet>
          <group>
            <field name="name"/>
            <field name="partner_id"/>
          </group>
          <notebook>
            <page string="Lines">
              <field name="line_ids">
                <list editable="bottom">
                  <field name="product_id"/>
                  <field name="qty"/>
                </list>
              </field>
            </page>
          </notebook>
        </sheet>
        <chatter/>
      </form>
    </field>
  </record>

  <record id="view_my_record_list" model="ir.ui.view">
    <field name="name">my.module.record.list</field>
    <field name="model">my.module.record</field>
    <field name="arch" type="xml">
      <list><field name="name"/><field name="state"/></list>
    </field>
  </record>

  <record id="action_my_record" model="ir.actions.act_window">
    <field name="name">My Records</field>
    <field name="res_model">my.module.record</field>
    <field name="view_mode">list,form</field>
  </record>

  <menuitem id="menu_my_module_root" name="My Module" sequence="60"/>
  <menuitem id="menu_my_record" parent="menu_my_module_root" action="action_my_record"/>
</odoo>
```

## 8. SEQUENCE / REFERENCE NUMBER (own numbering, not a native one)
```xml
<record id="seq_my_record" model="ir.sequence">
  <field name="name">My Record Sequence</field>
  <field name="code">my.module.record</field>
  <field name="prefix">MR/%(year)s/</field>
  <field name="padding">4</field>
</record>
```
```python
name = fields.Char(default=lambda s: s.env['ir.sequence'].next_by_code('my.module.record'))
```

---

## CRITICAL RULES
- **Own `_name`, own table.** Do not `_inherit` a native model to bolt on
  unrelated custom fields/logic — that couples your module to core upgrades
  and pollutes native tables. Reference native models with `Many2one`/`Many2many` instead.
- `_inherit` is only for genuine mixins (`mail.thread`, `mail.activity.mixin`,
  `portal.mixin`) or when you are intentionally extending an existing app's
  own model — not the default pattern for a new custom module.
- `ir.model.access.csv` is not optional — no row means every user gets a
  silent `AccessError`, including admins in some setups.
- Never inject `Pydantic`/FastAPI-style code into Odoo addons — Odoo model
  fields and `@api.constrains`/`@api.depends` are the only validation layer.
- For HTML chatter/audit notes, pass trusted `markupsafe.Markup` to `message_post` and escape every dynamic value; backfills must update an existing raw-HTML note in place instead of posting a duplicate.
- New tables need a migration story: fresh install is free; on existing DBs,
  new required fields need a `default=` or a post-init data hook.
- **Files must be complete per the prompt, not partial.** When a task asks
  for a model/view/module, write every file the feature needs end-to-end
  (model + security + view + action + menu, as applicable) — no stub
  methods, no `# TODO: implement`, no "add the rest later" placeholders,
  no half-written XML records. If scope is genuinely too large for one pass,
  say so explicitly and propose the split instead of silently truncating.
- Implement via `workflow.odoo_code_change` (server-owned read/write/verify/deploy).
  Never call hidden repo/git/odoosh tools directly.
