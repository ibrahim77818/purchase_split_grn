# -*- coding: utf-8 -*-
import datetime
import logging
from collections import OrderedDict

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare

_logger = logging.getLogger(__name__)


class PurchaseOrderLine(models.Model):
    _inherit = 'purchase.order.line'

    warehouse_id = fields.Many2one(
        'stock.warehouse', string='Receiving Warehouse',
        compute='_compute_warehouse_id', store=True, readonly=False, precompute=True,
        index='btree_not_null', check_company=True,
        help='Warehouse this line is received in. Defaults to the warehouse of the order\'s '
             '"Deliver To" operation; used to split receipts when "Split by Warehouse" is enabled.')
    location_dest_id = fields.Many2one(
        'stock.location', string='Destination Location',
        compute='_compute_location_dest_id', store=True, readonly=False, precompute=True,
        index='btree_not_null', check_company=True,
        domain="[('usage', 'in', ('internal', 'transit'))]",
        help='Where this line is received. Defaults to the receipt location of the '
             'order; used to split receipts when "Split by Destination Location" is enabled.')

    @api.depends('order_id.picking_type_id', 'display_type', 'is_downpayment')
    def _compute_warehouse_id(self):
        for line in self:
            line.warehouse_id = False if (line.display_type or line.is_downpayment) \
                else line.order_id.picking_type_id.warehouse_id

    @api.depends('order_id.picking_type_id', 'order_id.dest_address_id', 'warehouse_id',
                 'display_type', 'is_downpayment')
    def _compute_location_dest_id(self):
        """Default to the receipt location of the line's warehouse: the order's
        own receipt location in its warehouse, or the receipt location of the
        other warehouse's receipt operation. Reordering-rule lines keep that
        default too: their moves still reach the rule's sub-location through
        the standard purchase_stock logic."""
        Location = self.env['stock.location']
        for line in self:
            order = line.order_id
            if line.display_type or line.is_downpayment or not order or not order.picking_type_id:
                line.location_dest_id = False
                continue
            order_warehouse = order.picking_type_id.warehouse_id
            if line.warehouse_id and order_warehouse and line.warehouse_id != order_warehouse \
                    and not order.dest_address_id:
                default = line.warehouse_id.in_type_id.default_location_dest_id
            else:
                default = Location.browse(order._get_destination_location())
            # dropship / customer destinations are not split by location
            line.location_dest_id = default if default.usage in ('internal', 'transit') else False

    def _check_split_grn_location_warehouse(self):
        """The line's location must belong to the line's warehouse.

        Validated on explicit user input (create/write) rather than with
        @api.constrains: when the order's operation type changes, warehouse
        and location are recomputed one after the other and a field-level
        constraint would fire on the intermediate state."""
        for line in self.filtered('location_dest_id'):
            expected = line.warehouse_id or line.order_id.picking_type_id.warehouse_id
            location_warehouse = line.location_dest_id.warehouse_id
            if expected and location_warehouse and expected != location_warehouse:
                raise ValidationError(_(
                    'The destination location "%(location)s" of line "%(line)s" belongs to warehouse '
                    '"%(loc_wh)s", but the line is received in warehouse "%(order_wh)s".',
                    location=line.location_dest_id.display_name, line=line.name,
                    loc_wh=location_warehouse.name, order_wh=expected.name))

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        checked = self.browse([
            line.id for line, vals in zip(lines, vals_list)
            if 'location_dest_id' in vals or 'warehouse_id' in vals])
        checked._check_split_grn_location_warehouse()
        return lines

    # ------------------------------------------------------------------
    # Split keys
    # ------------------------------------------------------------------

    def _split_grn_picking_type(self):
        """Receipt operation type of this line: the order's own one, or the
        receipt operation of the line's warehouse when it is another one."""
        self.ensure_one()
        order = self.order_id
        order_type = order.picking_type_id
        if order._split_grn_warehouse_applicable() and self.warehouse_id \
                and self.warehouse_id != order_type.warehouse_id:
            receipt_type = self.warehouse_id.in_type_id
            if not receipt_type or not receipt_type.active:
                raise UserError(_(
                    'Warehouse "%s" has no active receipt operation type.', self.warehouse_id.name))
            return receipt_type
        return order_type

    def _split_grn_effective_location(self, picking_type=None):
        self.ensure_one()
        order = self.order_id
        picking_type = picking_type or self._split_grn_picking_type()
        location = self.location_dest_id
        if order._split_grn_location_applicable() and location \
                and (not location.warehouse_id or location.warehouse_id == picking_type.warehouse_id):
            return location
        if picking_type != order.picking_type_id:
            return picking_type.default_location_dest_id
        return self.env['stock.location'].browse(order._get_destination_location())

    def _split_grn_key(self):
        """(arrival day or False, destination location id, receipt type id)."""
        self.ensure_one()
        order = self.order_id
        date_key = False
        if order.split_grn_by_date_active:
            date_key = order._split_grn_local_date(self.date_planned or order.date_planned)
        picking_type = self._split_grn_picking_type()
        return (date_key, self._split_grn_effective_location(picking_type).id, picking_type.id)

    def _split_grn_group_by_key(self):
        groups = OrderedDict()
        for line in self:
            key = line._split_grn_key()
            groups.setdefault(key, self.browse())
            groups[key] |= line
        Location = self.env['stock.location']
        Type = self.env['stock.picking.type']
        return sorted(
            groups.items(),
            key=lambda item: (item[0][0] or datetime.date.min,
                              Type.browse(item[0][2]).warehouse_id.name or '',
                              Location.browse(item[0][1]).complete_name or ''))

    # ------------------------------------------------------------------
    # Stock moves
    # ------------------------------------------------------------------

    def _prepare_stock_move_vals(self, picking, price_unit, product_uom_qty, product_uom):
        vals = super()._prepare_stock_move_vals(picking, price_unit, product_uom_qty, product_uom)
        if self.order_id.split_grn_active and picking and picking.location_dest_id:
            Location = self.env['stock.location']
            target = picking.location_dest_id
            current = Location.browse(vals.get('location_dest_id'))
            picking_type = picking.picking_type_id
            changed = False
            # keep a more specific child location (e.g. a reordering rule's)
            if current != target and not current._child_of(target):
                vals['location_dest_id'] = target.id
                changed = True
            if picking_type and picking_type != self.order_id.picking_type_id:
                vals['picking_type_id'] = picking_type.id
                vals['warehouse_id'] = picking_type.warehouse_id.id
                changed = True
            if changed:
                final = self._split_grn_final_location(
                    Location.browse(vals['location_dest_id']), picking_type,
                    Location.browse(vals.get('location_final_id')))
                vals['location_final_id'] = final.id
                if final._child_of(Location.browse(vals['location_dest_id'])):
                    vals['location_dest_id'] = final.id
        return vals

    def _split_grn_final_location(self, location_dest, picking_type, current_final):
        """Final location (v18 ``location_final_id``) of a move sent to
        ``location_dest`` through ``picking_type``.

        The forecast of not-yet-received goods is computed on the final
        location, so it must follow the line to its warehouse/location:

        * a final location already inside the destination (e.g. a reordering
          rule's sub-location) is kept;
        * a receipt into the operation type's default destination (e.g. the
          Input location of a 2/3-step warehouse) keeps the chain going to
          that warehouse's stock, like standard Odoo;
        * otherwise the line's chosen location is the final location."""
        self.ensure_one()
        if current_final and current_final._child_of(location_dest) \
                and (not picking_type.warehouse_id
                     or current_final.warehouse_id == picking_type.warehouse_id):
            return current_final
        if location_dest == picking_type.default_location_dest_id:
            stock = picking_type.warehouse_id.lot_stock_id
            if stock and not location_dest._child_of(stock):
                return stock
        return location_dest

    def _create_or_update_picking(self):
        split_lines = self.filtered(lambda l: l.order_id.split_grn_active)
        super(PurchaseOrderLine, self - split_lines)._create_or_update_picking()
        created_by_order = {}
        for line in split_lines:
            if not (line.product_id and line.product_id.type == 'consu'):
                continue
            rounding = line.product_uom_id.rounding
            if float_compare(line.product_qty, line.qty_invoiced, precision_rounding=rounding) < 0 \
                    and line.invoice_lines:
                line.invoice_lines[0].move_id.activity_schedule(
                    'mail.mail_activity_data_warning',
                    note=_('The quantities on your purchase order indicate less than billed. '
                           'You should ask for a refund.'),
                    user_id=self.env.uid)
            order = line.order_id.with_company(line.company_id)
            key = line._split_grn_key()
            picking = order._split_grn_find_picking(key, preferred=line.move_ids.picking_id)
            if not picking:
                if not line.product_qty > line.qty_received:
                    continue
                picking = order._split_grn_new_picking(key)
                created_by_order.setdefault(order, self.env['stock.picking'])
                created_by_order[order] |= picking
            moves = line._create_stock_moves(picking)
            moves._action_confirm()._action_assign()
        for order, pickings in created_by_order.items():
            removed = order._split_grn_cleanup_pickings(pickings)
            order._split_grn_post_origin(pickings - removed)

    # ------------------------------------------------------------------
    # Post-confirmation maintenance
    # ------------------------------------------------------------------

    def write(self, vals):
        # Any write may change a line's split key: directly (date, location) or
        # through recomputation (e.g. a quantity change recomputing date_planned).
        previous_keys = {}
        for line in self:
            if line.order_id.state == 'purchase' and not line.display_type and not line.is_downpayment \
                    and line.order_id.split_grn_active:
                previous_keys[line] = line._split_grn_key()
        result = super().write(vals)
        if 'location_dest_id' in vals or 'warehouse_id' in vals:
            self._check_split_grn_location_warehouse()
        if previous_keys:
            changed = self.browse([
                line.id for line, key in previous_keys.items() if line._split_grn_key() != key])
            if changed:
                changed._split_grn_reroute_moves()
        return result

    def _split_grn_movable_moves(self):
        """Open incoming moves of the line that nobody has started processing.

        An incoming move's ``quantity`` is pre-filled with its demand
        when it is assigned, so a move counts as being processed when it (or
        one of its move lines) is marked *picked*, or when its quantity was
        edited away from the demand. Such moves are never moved: rerouting
        unreserves the move and would reset the counted quantity."""
        self.ensure_one()

        def untouched(move):
            if move.picked or any(move.move_line_ids.mapped('picked')):
                return False
            # supplier moves are fully available once assigned; a quantity
            # differing from the demand means somebody typed a count
            if move.state in ('assigned', 'partially_available') and float_compare(
                    move.quantity, move.product_uom_qty,
                    precision_rounding=move.product_uom.rounding) != 0:
                return False
            return True

        return self.move_ids.filtered(
            lambda m: m.state not in ('done', 'cancel')
            and m.picking_id
            and m.location_id.usage == 'supplier'
            and m.location_dest_id.usage != 'supplier'
            and untouched(m))

    def _split_grn_reroute_moves(self):
        """Move the open stock moves of lines whose split key changed into the
        receipt matching their new key, then delete receipts left empty."""
        Move = self.env['stock.move']
        for order in self.order_id:
            order = order.with_company(order.company_id)
            buckets = OrderedDict()
            for line in self.filtered(lambda l: l.order_id == order):
                key = line._split_grn_key()
                moves = line._split_grn_movable_moves().filtered(
                    lambda m: not order._split_grn_picking_matches(m.picking_id, key))
                if not moves:
                    continue
                chained = moves.filtered(
                    lambda m: m.move_dest_ids
                    and m.location_dest_id.id != key[1]
                    and not (m.location_dest_id.parent_path or '').startswith(
                        self.env['stock.location'].browse(key[1]).parent_path or '\0'))
                if chained:
                    raise UserError(_(
                        'The destination of line "%s" cannot be changed because its receipt is '
                        'already linked to further operations (multi-step reception or '
                        'make-to-order). Change it directly on the transfer instead.', line.name))
                buckets.setdefault(key, Move)
                buckets[key] |= moves

            sources = created = self.env['stock.picking']
            for key, moves in buckets.items():
                moves = moves.exists()
                source_pickings = moves.picking_id
                sources |= source_pickings
                moves._do_unreserve()
                target = order._split_grn_find_picking(key)
                if not target:
                    # a receipt can only be re-keyed within its own operation type
                    # (another warehouse means another sequence and routes)
                    reusable = source_pickings.filtered(
                        lambda p: not (p.move_ids - moves) and p.picking_type_id.id == key[2])
                    if reusable:
                        # the receipt only holds these moves: re-key it instead of
                        # creating a new one, so its reference is kept
                        target = reusable[0]
                        rekey_vals = {'location_dest_id': key[1], 'split_grn_date': key[0] or False}
                        target.write(rekey_vals)
                    else:
                        target = order._split_grn_new_picking(key)
                        created |= target
                target_location = target.location_dest_id
                target_type = target.picking_type_id
                for move in moves:
                    move_vals = {'picking_id': target.id}
                    if move.picking_type_id != target_type:
                        move_vals['picking_type_id'] = target_type.id
                        move_vals['warehouse_id'] = target_type.warehouse_id.id
                    if move.location_dest_id != target_location \
                            and not move.location_dest_id._child_of(target_location):
                        move_vals['location_dest_id'] = target_location.id
                    new_dest = self.env['stock.location'].browse(
                        move_vals.get('location_dest_id', move.location_dest_id.id))
                    final = move.purchase_line_id._split_grn_final_location(
                        new_dest, target_type, move.location_final_id)
                    if final != move.location_final_id:
                        move_vals['location_final_id'] = final.id
                    if final != new_dest and final._child_of(new_dest):
                        move_vals['location_dest_id'] = final.id
                    # the move's destination is recomputed from its receipt, then
                    # follows a final location below it (stock.move compute)
                    move_vals.setdefault('location_dest_id', move.location_dest_id.id)
                    if order.split_grn_by_date_active and move.purchase_line_id.date_planned:
                        move_vals['date'] = move.purchase_line_id.date_planned
                    move.write(move_vals)
                moves._action_assign()
                _logger.info('purchase_split_grn: %s moved %s move(s) to receipt %s',
                             order.name, len(moves), target.name)
            removed = order._split_grn_cleanup_pickings(sources | created)
            order._split_grn_post_origin(created - removed)
