# -*- coding: utf-8 -*-
import datetime
import logging

import pytz

from odoo import SUPERUSER_ID, api, fields, models

_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _inherit = 'purchase.order'

    split_grn_active = fields.Boolean(
        string='Receipt Splitting Active', compute='_compute_split_grn_flags')
    split_grn_by_date_active = fields.Boolean(
        string='Split Receipts by Date', compute='_compute_split_grn_flags')
    split_grn_location_active = fields.Boolean(
        string='Split Receipts by Location', compute='_compute_split_grn_flags')
    split_grn_warehouse_active = fields.Boolean(
        string='Split Receipts by Warehouse', compute='_compute_split_grn_flags')

    @api.depends('company_id.split_grn_enabled', 'company_id.split_grn_by_date',
                 'company_id.split_grn_by_location', 'company_id.split_grn_by_warehouse')
    def _compute_split_grn_flags(self):
        for order in self:
            company = order.company_id or self.env.company
            enabled = company.split_grn_enabled
            order.split_grn_by_date_active = enabled and company.split_grn_by_date
            order.split_grn_location_active = enabled and company.split_grn_by_location
            order.split_grn_warehouse_active = enabled and company.split_grn_by_warehouse
            order.split_grn_active = (order.split_grn_by_date_active
                                      or order.split_grn_location_active
                                      or order.split_grn_warehouse_active)

    # ------------------------------------------------------------------
    # Grouping helpers
    # ------------------------------------------------------------------

    def _split_grn_receives_in_stock(self):
        """Receipts go into the company's own stock (not dropship / customer)."""
        self.ensure_one()
        return not self.dest_address_id and \
            self.picking_type_id.default_location_dest_id.usage in ('internal', 'transit')

    def _split_grn_location_applicable(self):
        """Per-line locations only make sense for receipts into own stock."""
        self.ensure_one()
        return self.split_grn_location_active and self._split_grn_receives_in_stock()

    def _split_grn_warehouse_applicable(self):
        """Per-line warehouses need receipts into own stock and a warehouse
        on the order's operation type."""
        self.ensure_one()
        return self.split_grn_warehouse_active and self._split_grn_receives_in_stock() \
            and bool(self.picking_type_id.warehouse_id)

    def _split_grn_timezone(self):
        self.ensure_one()
        tz_name = self.user_id.tz or self.env.user.tz or 'UTC'
        try:
            return pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            return pytz.utc

    def _split_grn_local_date(self, value):
        """Calendar day (in the buyer's timezone) of a UTC datetime."""
        self.ensure_one()
        if not value:
            return False
        if isinstance(value, datetime.datetime):
            return pytz.utc.localize(value).astimezone(self._split_grn_timezone()).date()
        return fields.Date.to_date(value)

    def _split_grn_open_pickings(self):
        self.ensure_one()
        return self.picking_ids.filtered(
            lambda p: p.state not in ('done', 'cancel')
            and p.location_dest_id.usage in ('internal', 'transit', 'customer'))

    def _split_grn_picking_matches(self, picking, key):
        date_key, location_id, picking_type_id = key
        if picking.location_dest_id.id != location_id or picking.picking_type_id.id != picking_type_id:
            return False
        if self.split_grn_by_date_active and (picking.split_grn_date or False) != date_key:
            return False
        return True

    def _split_grn_find_picking(self, key, preferred=None, exclude=None):
        """Return an open receipt of this order matching ``key``.
        ``preferred`` pickings (e.g. those already holding the line's moves)
        are checked first so quantity updates stay where they are."""
        self.ensure_one()
        candidates = (preferred or self.env['stock.picking']).filtered(
            lambda p: p.state not in ('done', 'cancel')) | self._split_grn_open_pickings()
        if exclude:
            candidates -= exclude
        for picking in candidates:
            if self._split_grn_picking_matches(picking, key):
                return picking
        return self.env['stock.picking']

    def _split_grn_prepare_picking(self, key):
        self.ensure_one()
        date_key, location_id, picking_type_id = key
        vals = self._prepare_picking()
        vals['location_dest_id'] = location_id
        vals['picking_type_id'] = picking_type_id
        if date_key:
            vals['split_grn_date'] = date_key
        return vals

    def _split_grn_new_picking(self, key):
        self.ensure_one()
        picking = self.env['stock.picking'].with_user(SUPERUSER_ID).create(
            self._split_grn_prepare_picking(key))
        return self.env['stock.picking'].browse(picking.id)

    def _split_grn_post_origin(self, pickings):
        self.ensure_one()
        for picking in pickings:
            picking.message_post_with_source(
                'mail.message_origin_link',
                render_values={'self': picking, 'origin': self},
                subtype_xmlid='mail.mt_note',
            )

    @api.model
    def _split_grn_cleanup_pickings(self, pickings):
        """Remove receipts left without any stock move. Done and cancelled
        receipts are never touched."""
        empty = pickings.exists().filtered(
            lambda p: p.state not in ('done', 'cancel') and not p.move_ids)
        if empty:
            _logger.info('purchase_split_grn: removing empty receipts %s', empty.mapped('name'))
            empty.sudo().unlink()
        return empty

    # ------------------------------------------------------------------
    # Picking generation
    # ------------------------------------------------------------------

    def _create_picking(self):
        split_orders = self.filtered(
            lambda o: o.state in ('purchase', 'done') and o.split_grn_active)
        res = super(PurchaseOrder, self - split_orders)._create_picking()
        for order in split_orders:
            order.with_company(order.company_id)._split_grn_create_pickings()
        return res

    def _split_grn_create_pickings(self):
        self.ensure_one()
        lines = self.order_line.filtered(
            lambda l: not l.display_type and l.product_id.type in ('product', 'consu'))
        if not lines:
            return self.env['stock.picking']

        touched = created = self.env['stock.picking']
        moves = self.env['stock.move']
        for key, group_lines in lines._split_grn_group_by_key():
            picking = self._split_grn_find_picking(
                key, preferred=group_lines.move_ids.picking_id)
            if not picking:
                picking = self._split_grn_new_picking(key)
                created |= picking
            moves |= group_lines._create_stock_moves(picking)
            touched |= picking

        removed = self._split_grn_cleanup_pickings(created)
        created -= removed
        touched -= removed

        moves = moves.exists().filtered(lambda m: m.state not in ('done', 'cancel'))._action_confirm()
        for picking in moves.picking_id:
            seq = 0
            for move in sorted(moves.filtered(lambda m: m.picking_id == picking),
                               key=lambda m: m.date):
                seq += 5
                move.sequence = seq
        moves._action_assign()
        forward_pickings = self.env['stock.picking']._get_impacted_pickings(moves)
        (touched | forward_pickings).action_confirm()
        self._split_grn_post_origin(created)
        return touched
