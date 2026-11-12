from odoo import fields, models


class TestModel(models.Model):
    _name = "agent.test.model"
    _description = "Agent Test Model"

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
