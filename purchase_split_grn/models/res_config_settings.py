# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    split_grn_enabled = fields.Boolean(
        related='company_id.split_grn_enabled', readonly=False)
    split_grn_by_date = fields.Boolean(
        related='company_id.split_grn_by_date', readonly=False)
    split_grn_by_location = fields.Boolean(
        related='company_id.split_grn_by_location', readonly=False)
    split_grn_by_warehouse = fields.Boolean(
        related='company_id.split_grn_by_warehouse', readonly=False)
    # UI-only: shows the expected arrival column on purchase order lines by default.
    group_split_grn_by_date = fields.Boolean(
        string='Show Line Arrival Dates',
        implied_group='purchase_split_grn.group_split_grn_by_date')

    def set_values(self):
        for settings in self:
            settings.group_split_grn_by_date = bool(
                settings.split_grn_enabled and settings.split_grn_by_date)
        return super().set_values()
