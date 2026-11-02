# -*- coding: utf-8 -*-
from odoo import fields, models


class StockPicking(models.Model):
    _inherit = 'stock.picking'

    split_grn_date = fields.Date(
        string='Grouped Arrival Date', readonly=True, copy=False, index=True,
        help='Expected arrival date this receipt groups purchase order lines by.')
