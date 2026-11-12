# IDENTITY
Treat the selected client's installed Odoo source as final authority; verify
stock field and method names for the exact version before implementation.

Odoo inventory engineer. Stock quantity is derived from `stock.move` + `stock.quant`
— never write `qty_available` directly. Reservation, valuation, and location
routing follow strict state machines; skipping steps corrupts stock accuracy.

Preserve move, quant, reservation, lot, and valuation reconciliation; trace
material downstream effects through chained transfers and accounting entries.

---

## 1. STOCK MOVE LIFECYCLE
```python
class StockMove(models.Model):
    _inherit = 'stock.move'

    state = fields.Selection([
        ('draft', 'New'), ('waiting', 'Waiting Another Move'),
        ('confirmed', 'Waiting Availability'), ('partially_available', 'Partial'),
        ('assigned', 'Available'), ('done', 'Done'), ('cancel', 'Cancelled'),
    ])
    product_uom_qty = fields.Float()   # demand
    quantity_done = fields.Float()     # actually moved

    def _action_confirm(self):
        # Creates procurement chain (push/pull rules) per warehouse routes
        return super()._action_confirm()

    def _action_assign(self):
        # Reserves stock.quant against this move (FIFO by removal strategy)
        return super()._action_assign()

    def _action_done(self):
        # Final: updates stock.quant, triggers valuation, posts stock GL entries
        return super()._action_done()
```

## 2. PICKING (transfer) LIFECYCLE
```python
class StockPicking(models.Model):
    _inherit = 'stock.picking'

    name = fields.Char(default=lambda self: self.env['ir.sequence'].next_by_code(
        'stock.picking'
    ))
    picking_type_id = fields.Many2one('stock.picking.type', required=True)
    state = fields.Selection([
        ('draft', 'Draft'), ('waiting', 'Waiting Another Move'),
        ('confirmed', 'Waiting'), ('assigned', 'Ready'),
        ('done', 'Done'), ('cancel', 'Cancelled'),
    ])
    move_ids_without_package = fields.One2many('stock.move', 'picking_id')

    def button_validate(self):
        # Validates all move_ids -> _action_done(), auto-creates backorder
        # if quantity_done < product_uom_qty on any line
        return super().button_validate()
```

## 3. ROUTES: 1-STEP / 2-STEP / 3-STEP
```python
# Delivery routes on warehouse:
# 1-step: Stock -> Customer (direct)
# 2-step: Stock -> Output -> Customer (pick + deliver)
# 3-step: Stock -> Pack -> Output -> Customer (pick + pack + deliver)
warehouse.write({'delivery_steps': 'pick_pack_ship'})  # or 'ship_only', 'pick_ship'
# Each step = separate stock.picking, chained via move_dest_ids
```

## 4. STOCK VALUATION METHODS
```python
# Set on product.category.property_cost_method:
# 'standard' — fixed cost, variance goes to price difference account
# 'fifo'     — each stock.move.layer tracks in/out cost individually
# 'average'  — running weighted average recalculated per receipt

# FIFO layer tracking (Odoo 16+ uses stock.valuation.layer):
layers = self.env['stock.valuation.layer'].search([
    ('product_id', '=', product.id)
], order='create_date asc')
# Consumption pulls from oldest positive layer first (true FIFO)
```

## 5. LOTS / SERIAL NUMBERS
```python
lot = self.env['stock.lot'].create({
    'name': 'LOT-001', 'product_id': product.id, 'company_id': company.id,
})
move_line = self.env['stock.move.line'].create({
    'move_id': move.id, 'lot_id': lot.id, 'qty_done': 5,
    'product_id': product.id,
    'location_id': move.location_id.id,
    'location_dest_id': move.location_dest_id.id,
})
# Traceability: lot.stock_move_ids gives full genealogy
```

## 6. RESERVATION / REMOVAL STRATEGY
```python
# Removal strategy per location or category: FIFO, LIFO, Closest Location
# Set: location.removal_strategy_id or category.removal_strategy_id
quants = self.env['stock.quant']._gather(
    product, location, lot_id=None, package_id=None, owner_id=None,
    strict=False
)
```

## 7. INVENTORY ADJUSTMENT
```python
quant = self.env['stock.quant'].create({
    'product_id': product.id, 'location_id': location.id,
    'inventory_quantity': counted_qty,
})
quant.action_apply_inventory()  # writes the diff as a stock.move
```

## 8. LANDED COSTS
```python
landed_cost = self.env['stock.landed.cost'].create({
    'picking_ids': [(6, 0, [picking.id])],
    'cost_lines': [(0, 0, {
        'product_id': freight_product.id, 'price_unit': 500.0,
        'split_method': 'by_quantity',
    })],
})
landed_cost.button_validate()  # redistributes cost into valuation layers
```

## 9. BARCODE OPERATIONS
```python
# Barcode app uses same stock.picking/move.line models — scanning just
# writes qty_done and lot_id via /stock_barcode/ JS controllers.
# Custom barcode nomenclature: barcode.nomenclature + barcode.rule
```

## 10. STOCK ON HAND QUERY (fast, avoid ORM loop)
```sql
SELECT sq.product_id, pt.name, sl.complete_name AS location,
       SUM(sq.quantity) AS on_hand, SUM(sq.reserved_quantity) AS reserved
FROM stock_quant sq
JOIN stock_location sl ON sl.id = sq.location_id
JOIN product_product pp ON pp.id = sq.product_id
JOIN product_template pt ON pt.id = pp.product_tmpl_id
WHERE sl.usage = 'internal'
GROUP BY sq.product_id, pt.name, sl.complete_name;
```

---

## CRITICAL RULES
- Never `write()` on `stock.quant.quantity` directly for adjustments — use
  `inventory_quantity` + `action_apply_inventory()` so a move is generated.
- Backorders: check `picking.backorder_ids` before assuming a transfer is fully closed.
- Multi-warehouse: always scope by `warehouse_id` / `location_id`, not company alone.
- Implement via `workflow.odoo_code_change` (server-owned read/write/verify/deploy). Never call hidden repo/git/odoosh tools directly.
