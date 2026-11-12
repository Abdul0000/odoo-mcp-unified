# Odoo Point of Sale (POS) Development

## IDENTITY
Treat the selected client's installed POS source and loaded asset bundle as
final authority; do not infer POS/OWL contracts from an Odoo version label.

Odoo POS engineer. POS runs offline-first in the browser (OWL framework) and
syncs to `pos.order` on session close/sync — never assume server state is
live during an active session. Payment methods, pricelists, and taxes are
loaded into the POS's in-memory `models` cache at session start.

Apply the `odoo-view-development` OWL/QWeb contract: verify installed-version
templates, widgets, registries, dependencies, XPath targets, assets, and final
POS rendering; never copy a template or bundle name from another version.

Keep offline retries idempotent by order/reference identity. Verify the
frontend payload, backend order creation, payment state, stock/accounting
handoff, loaded asset hash, and duplicate component registration before
claiming a POS change fixed.

Model relevant offline, reconnect, duplicate-order, stale-session, payment,
and partial-sync failures; reconcile the complete browser-to-GL handoff.

---

## 1. MODULE SETUP
```python
# __manifest__.py
{
    'name': 'POS Custom',
    'depends': ['point_of_sale'],
    'data': ['views/pos_config_views.xml'],
    'assets': {
        'point_of_sale._assets_pos': [
            'pos_custom/static/src/js/pos_custom.js',
            'pos_custom/static/src/scss/pos_custom.scss',
        ],
    },
}
```
**Version note**: asset bundle key is `point_of_sale._assets_pos` in 17+.
Older snippets showing `point_of_sale.assets` are pre-17 — verify against
the target version's `point_of_sale/__manifest__.py` assets dict before use.

## 2. SESSION LIFECYCLE
```python
class PosSession(models.Model):
    _inherit = 'pos.session'

    state = fields.Selection([
        ('opening_control', 'Opening'), ('opened', 'In Progress'),
        ('closing_control', 'Closing'), ('closed', 'Closed'),
    ])

    def action_pos_session_closing_control(self):
        # Validates cash counts, creates account.bank.statement entries
        return super().action_pos_session_closing_control()
```
One open session per `pos.config` at a time — starting a new one while
another is `opened` will fail; check `pos_config.current_session_id` first.

## 3. ORDER MODEL & GL SYNC
```python
class PosOrder(models.Model):
    _inherit = 'pos.order'

    state = fields.Selection([
        ('draft', 'New'), ('paid', 'Paid'), ('done', 'Posted'),
        ('invoiced', 'Invoiced'), ('cancel', 'Cancelled'),
    ])
    lines = fields.One2many('pos.order.line', 'order_id')
    payment_ids = fields.One2many('pos.payment', 'pos_order_id')

    def _create_order_picking(self):
        # Auto-creates stock.picking for stockable products on order confirm
        return super()._create_order_picking()
```
`pos.order` → `account.move` happens on session close (batch) via
`_create_invoice()` per order or a consolidated statement entry — don't
assume a GL entry exists until the session is fully closed.

## 4. OWL FRONTEND — CUSTOM PAYMENT SCREEN BUTTON (17+)
```javascript
/** @odoo-module **/
import { PaymentScreen } from "@point_of_sale/app/screens/payment_screen/payment_screen";
import { patch } from "@web/core/utils/patch";

patch(PaymentScreen.prototype, {
    async customAction() {
        const order = this.pos.get_order();
        // Access reactive state directly — no explicit re-render call needed,
        // OWL's reactivity system re-renders on state mutation.
        order.custom_field = "value";
    },
});
```
`patch()` on the class prototype is the 17+ pattern — pre-17 code using
`odoo.define()` + `Registries` is deprecated, don't mix the two styles.

## 5. RECEIPT TEMPLATE (QWeb, XML-based in 17+)
```xml
<t t-name="pos_custom.ReceiptExtra" t-inherit="point_of_sale.OrderReceipt"
   t-inherit-mode="extension">
  <xpath expr="//div[hasclass('pos-receipt-order-data')]" position="after">
    <div class="pos-receipt-custom">
      <t t-esc="props.data.custom_note"/>
    </div>
  </xpath>
</t>
```

## 6. CUSTOM PAYMENT METHOD
```python
payment_method = self.env['pos.payment.method'].create({
    'name': 'Custom Wallet',
    'journal_id': journal.id,          # links to accounting journal for GL
    'is_cash_count': False,
    'use_payment_terminal': False,     # set a terminal provider string if integrating hardware
})
# Attach to POS config so it's available at the register:
pos_config.write({'payment_method_ids': [(4, payment_method.id)]})
```

## 7. HARDWARE INTEGRATION (IoT Box)
```python
# Hardware (barcode scanner, receipt printer, cash drawer, scale) is
# proxied through the IoT Box — POS talks to it via /hw_proxy/ REST calls
# from the browser, NOT through the Odoo server ORM. Server-side code only
# configures which iot.device is assigned to which pos.config.
pos_config.write({'iface_printer_id': printer_device.id})
```

## 8. RESTAURANT FEATURES (floors/tables)
```python
class RestaurantTable(models.Model):
    _inherit = 'restaurant.table'
    floor_id = fields.Many2one('restaurant.floor', required=True)
    seats = fields.Integer(default=4)

# Orders link to a table via pos.order.table_id; split-bill and course
# management (restaurant.order.course) are separate models layered on top.
```

## 9. LOYALTY / DISCOUNTS (17+ unified loyalty.program)
```python
program = self.env['loyalty.program'].create({
    'name': 'Points Program', 'program_type': 'loyalty',
    'trigger': 'auto', 'applies_on': 'both',
})
# Rules (loyalty.rule) define earn logic; rewards (loyalty.reward) define
# redemption — both linked via program_id. Coupons live on loyalty.card.
```

## 10. DAILY SALES / Z-REPORT (SQL, fast)
```sql
SELECT pc.name AS pos_config, DATE(po.date_order) AS sale_date,
       SUM(po.amount_total) AS total_sales,
       SUM(po.amount_tax) AS total_tax,
       COUNT(*) AS order_count
FROM pos_order po
JOIN pos_config pc ON pc.id = po.config_id
WHERE po.state IN ('paid', 'done', 'invoiced')
GROUP BY pc.name, DATE(po.date_order)
ORDER BY sale_date DESC;
```

---

## CRITICAL RULES
- Never assume server-side data is current during an active session — the
  POS UI runs against its own in-browser cache until sync/close.
- One open session per `pos.config` — always check
  `pos_config.current_session_id` before opening a new one.
- Never define or override `_load_pos_data_fields` on `pos.config`. Read the
  installed `point_of_sale` source and use that version's supported POS
  loader/session mechanism.
- Use `patch()` on component prototypes (17+) for POS UI customization, not
  the deprecated `odoo.define()` module registry pattern.
- Hardware calls go through the IoT Box proxy, not the Odoo ORM.
- Implement via `workflow.odoo_code_change` (server-owned read/write/verify/deploy). Never call hidden repo/git/odoosh tools directly.
