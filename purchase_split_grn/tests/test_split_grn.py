# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, tagged
from odoo.tests.common import TransactionCase


@tagged('post_install', '-at_install', 'purchase_split_grn')
class TestPurchaseSplitGrn(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        cls.env.user.tz = 'Asia/Riyadh'
        cls.env.user.groups_id += cls.env.ref('stock.group_stock_multi_locations')
        cls.company = cls.env.company
        cls.warehouse = cls.env['stock.warehouse'].search([('company_id', '=', cls.company.id)], limit=1)
        cls.stock = cls.warehouse.lot_stock_id
        Location = cls.env['stock.location']
        cls.shelf_a = Location.create({'name': 'Shelf A', 'location_id': cls.stock.id, 'usage': 'internal'})
        cls.shelf_b = Location.create({'name': 'Shelf B', 'location_id': cls.stock.id, 'usage': 'internal'})
        cls.other_wh = cls.env['stock.warehouse'].create({'name': 'Other WH', 'code': 'OWH'})
        cls.vendor = cls.env['res.partner'].create({'name': 'Split Vendor'})
        Product = cls.env['product.product']
        cls.p1 = Product.create({'name': 'P1', 'type': 'product'})
        cls.p2 = Product.create({'name': 'P2', 'type': 'product'})
        cls.p3 = Product.create({'name': 'P3', 'type': 'consu'})
        cls.p4 = Product.create({'name': 'P4', 'type': 'product'})
        cls.service = Product.create({'name': 'Freight', 'type': 'service'})
        # 09:00 Riyadh time = 06:00 UTC, so the local day equals the UTC day
        base = fields.Datetime.now().replace(hour=6, minute=0, second=0, microsecond=0)
        cls.d1 = base + timedelta(days=3)
        cls.d2 = base + timedelta(days=7)
        cls.d3 = base + timedelta(days=10)

    # helpers ----------------------------------------------------------
    def _configure(self, enabled=True, by_date=False, by_location=False, by_warehouse=False):
        self.company.write({
            'split_grn_enabled': enabled,
            'split_grn_by_date': by_date,
            'split_grn_by_location': by_location,
            'split_grn_by_warehouse': by_warehouse,
        })

    def _po(self, lines, confirm=True):
        vals = []
        for spec in lines:
            line = {
                'product_id': spec['product'].id,
                'product_qty': spec.get('qty', 10.0),
                'price_unit': 5.0,
                'date_planned': spec.get('date', self.d1),
            }
            if spec.get('location'):
                line['location_dest_id'] = spec['location'].id
            vals.append((0, 0, line))
        po = self.env['purchase.order'].create({
            'partner_id': self.vendor.id,
            'picking_type_id': self.warehouse.in_type_id.id,
            'order_line': vals,
        })
        if confirm:
            po.button_confirm()
        return po

    def _picking_of(self, line):
        pickings = line.move_ids.filtered(lambda m: m.state != 'cancel').picking_id
        self.assertEqual(len(pickings), 1, 'line %s should sit in exactly one receipt' % line.name)
        return pickings

    def _local_date(self, dt):
        return (dt + timedelta(hours=3)).date()

    # configuration ------------------------------------------------------
    def test_00_settings_default_and_persist(self):
        company = self.env['res.company'].create({'name': 'Fresh Co'})
        self.assertFalse(company.split_grn_enabled)
        self.assertFalse(company.split_grn_by_date)
        self.assertFalse(company.split_grn_by_location)

        settings = self.env['res.config.settings'].create({
            'split_grn_enabled': True, 'split_grn_by_date': True, 'split_grn_by_location': True})
        settings.execute()
        self.assertTrue(self.company.split_grn_enabled)
        self.assertTrue(self.company.split_grn_by_date)
        self.assertTrue(self.company.split_grn_by_location)
        group = self.env.ref('purchase_split_grn.group_split_grn_by_date')
        self.assertIn(group, self.env.ref('base.group_user').implied_ids)

        settings = self.env['res.config.settings'].create({'split_grn_enabled': False})
        settings.execute()
        self.assertFalse(self.company.split_grn_enabled)
        self.assertNotIn(group, self.env.ref('base.group_user').implied_ids)

    def test_01_disabled_single_receipt(self):
        self._configure(enabled=False, by_date=True, by_location=True)
        po = self._po([
            {'product': self.p1, 'date': self.d1, 'location': self.shelf_a},
            {'product': self.p2, 'date': self.d2, 'location': self.shelf_b},
        ])
        self.assertEqual(len(po.picking_ids), 1)
        self.assertEqual(po.picking_ids.location_dest_id, self.stock)
        self.assertEqual(set(po.picking_ids.move_ids.mapped('location_dest_id').ids), {self.stock.id})

    def test_02_enabled_without_criteria(self):
        self._configure(enabled=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d2}])
        self.assertFalse(po.split_grn_active)
        self.assertEqual(len(po.picking_ids), 1)

    def test_03_line_location_default(self):
        self._configure(by_location=True)
        po = self._po([{'product': self.p1}], confirm=False)
        self.assertEqual(po.order_line.location_dest_id, self.warehouse.in_type_id.default_location_dest_id)
        with Form(po) as po_form:
            with po_form.order_line.new() as line:
                line.product_id = self.p2
                line.product_qty = 1
        self.assertEqual(po.order_line[1].location_dest_id, self.stock)

    # splitting ------------------------------------------------------------
    def test_10_split_by_date(self):
        self._configure(by_date=True)
        po = self._po([
            {'product': self.p1, 'date': self.d1},
            {'product': self.p2, 'date': self.d1 + timedelta(hours=5)},  # same day
            {'product': self.p3, 'date': self.d2},
            {'product': self.service, 'date': self.d3},  # services never create moves
        ])
        self.assertEqual(len(po.picking_ids), 2)
        l1, l2, l3, _l4 = po.order_line
        self.assertEqual(self._picking_of(l1), self._picking_of(l2))
        self.assertNotEqual(self._picking_of(l1), self._picking_of(l3))
        self.assertEqual(self._picking_of(l1).split_grn_date, self._local_date(self.d1))
        self.assertEqual(self._picking_of(l3).split_grn_date, self._local_date(self.d2))
        self.assertEqual(self._picking_of(l3).scheduled_date, self.d2)
        self.assertTrue(all(p.state == 'assigned' for p in po.picking_ids))
        self.assertEqual(po.incoming_picking_count, 2)

    def test_11_split_by_date_uses_buyer_timezone(self):
        """23:00 UTC is already the next day in Riyadh."""
        self._configure(by_date=True)
        late = self.d1.replace(hour=22, minute=30)       # 01:30 next day local
        next_morning = self.d1.replace(hour=6) + timedelta(days=1)
        po = self._po([{'product': self.p1, 'date': late}, {'product': self.p2, 'date': next_morning}])
        self.assertEqual(len(po.picking_ids), 1)
        self.assertEqual(po.picking_ids.split_grn_date, self._local_date(late))

    def test_12_split_by_location(self):
        self._configure(by_location=True)
        po = self._po([
            {'product': self.p1, 'location': self.shelf_a},
            {'product': self.p2, 'location': self.shelf_b},
            {'product': self.p3},  # default destination
            {'product': self.p4, 'location': self.shelf_a, 'date': self.d2},  # date ignored
        ])
        self.assertEqual(len(po.picking_ids), 3)
        l1, l2, l3, l4 = po.order_line
        self.assertEqual(self._picking_of(l1), self._picking_of(l4))
        self.assertEqual(self._picking_of(l1).location_dest_id, self.shelf_a)
        self.assertEqual(self._picking_of(l2).location_dest_id, self.shelf_b)
        self.assertEqual(self._picking_of(l3).location_dest_id, self.stock)
        for line in po.order_line:
            self.assertEqual(line.move_ids.location_dest_id, line.location_dest_id)
        self.assertFalse(any(po.picking_ids.mapped('split_grn_date')))

    def test_13_split_by_date_and_location(self):
        self._configure(by_date=True, by_location=True)
        po = self._po([
            {'product': self.p1, 'date': self.d1, 'location': self.shelf_a},
            {'product': self.p2, 'date': self.d1, 'location': self.shelf_b},
            {'product': self.p3, 'date': self.d2, 'location': self.shelf_a},
            {'product': self.p4, 'date': self.d1, 'location': self.shelf_a},
        ])
        self.assertEqual(len(po.picking_ids), 3)
        l1, l2, l3, l4 = po.order_line
        self.assertEqual(self._picking_of(l1), self._picking_of(l4))
        self.assertEqual(len(self._picking_of(l1).move_ids), 2)
        keys = {(p.split_grn_date, p.location_dest_id.id) for p in po.picking_ids}
        self.assertEqual(keys, {
            (self._local_date(self.d1), self.shelf_a.id),
            (self._local_date(self.d1), self.shelf_b.id),
            (self._local_date(self.d2), self.shelf_a.id),
        })
        # every move sits in a receipt whose key matches its line
        for line in po.order_line:
            picking = self._picking_of(line)
            self.assertEqual(picking.location_dest_id, line.location_dest_id)
            self.assertEqual(picking.split_grn_date, self._local_date(line.date_planned))
        total = sum(po.picking_ids.move_ids.mapped('product_uom_qty'))
        self.assertEqual(total, 40.0)

    def test_14_receive_one_split_receipt(self):
        self._configure(by_date=True, by_location=True)
        po = self._po([
            {'product': self.p1, 'date': self.d1, 'location': self.shelf_a},
            {'product': self.p2, 'date': self.d2, 'location': self.shelf_b},
        ])
        first = self._picking_of(po.order_line[0])
        first.move_ids.quantity_done = 10.0
        first.button_validate()
        self.assertEqual(first.state, 'done')
        self.assertEqual(po.order_line[0].qty_received, 10.0)
        self.assertEqual(po.order_line[1].qty_received, 0.0)
        self.assertEqual(po.receipt_status, 'partial')
        self.assertEqual(self.env['stock.quant']._get_available_quantity(self.p1, self.shelf_a), 10.0)

    def test_15_location_from_other_warehouse_rejected(self):
        self._configure(by_location=True)
        with self.assertRaises(ValidationError):
            self._po([{'product': self.p1, 'location': self.other_wh.lot_stock_id}], confirm=False)

    def test_16_cancel_order_cancels_all_receipts(self):
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d2}])
        pickings = po.picking_ids
        self.assertEqual(len(pickings), 2)
        po.button_cancel()
        self.assertTrue(all(p.state == 'cancel' for p in pickings))

    # post-confirmation maintenance ----------------------------------------
    def test_20_date_change_merges_and_cleans_up(self):
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d2}])
        l1, l2 = po.order_line
        p1, p2 = self._picking_of(l1), self._picking_of(l2)
        l2.date_planned = self.d1
        self.assertFalse(p2.exists(), 'emptied receipt must be removed')
        self.assertEqual(self._picking_of(l2), p1)
        self.assertEqual(po.picking_ids, p1)
        self.assertEqual(l2.move_ids.date, self.d1)
        self.assertEqual(l2.move_ids.date_deadline, self.d1)
        self.assertEqual(l2.move_ids.state, 'assigned')

    def test_21_date_change_rekeys_own_receipt(self):
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d2}])
        l2 = po.order_line[1]
        picking = self._picking_of(l2)
        name = picking.name
        l2.date_planned = self.d3
        self.assertTrue(picking.exists())
        self.assertEqual(self._picking_of(l2), picking, 'sole receipt is re-keyed, not replaced')
        self.assertEqual(picking.name, name)
        self.assertEqual(picking.split_grn_date, self._local_date(self.d3))
        self.assertEqual(picking.scheduled_date, self.d3)
        self.assertEqual(len(po.picking_ids), 2)

    def test_22_date_change_splits_shared_receipt(self):
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d1}])
        l1, l2 = po.order_line
        shared = self._picking_of(l1)
        l2.date_planned = self.d2
        self.assertEqual(len(po.picking_ids), 2)
        self.assertEqual(self._picking_of(l1), shared)
        new = self._picking_of(l2)
        self.assertNotEqual(new, shared)
        self.assertEqual(new.split_grn_date, self._local_date(self.d2))
        self.assertEqual(new.origin, po.name)
        self.assertEqual(new.state, 'assigned')

    def test_23_header_date_change_moves_all_lines(self):
        """Changing the order's expected arrival in the form updates every line."""
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d2}])
        with Form(po) as po_form:
            po_form.date_planned = self.d3
        self.assertEqual(po.order_line.mapped('date_planned'), [self.d3, self.d3])
        self.assertEqual(len(po.picking_ids), 1)
        self.assertEqual(po.picking_ids.split_grn_date, self._local_date(self.d3))
        self.assertEqual(len(po.picking_ids.move_ids), 2)

    def test_24_location_change_post_confirmation(self):
        self._configure(by_location=True)
        po = self._po([
            {'product': self.p1, 'location': self.shelf_a},
            {'product': self.p2, 'location': self.shelf_b},
        ])
        l1, l2 = po.order_line
        pa, pb = self._picking_of(l1), self._picking_of(l2)
        l2.location_dest_id = self.shelf_a
        self.assertFalse(pb.exists())
        self.assertEqual(self._picking_of(l2), pa)
        self.assertEqual(l2.move_ids.location_dest_id, self.shelf_a)
        self.assertEqual(l2.move_ids.move_line_ids.location_dest_id, self.shelf_a)
        # leaving a shared receipt for a new location creates a new one
        l2.location_dest_id = self.stock
        self.assertEqual(len(po.picking_ids), 2)
        self.assertEqual(self._picking_of(l2).location_dest_id, self.stock)
        self.assertEqual(self._picking_of(l1), pa)

    def test_25_processed_moves_are_not_moved(self):
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d2}])
        l2 = po.order_line[1]
        picking = self._picking_of(l2)
        l2.move_ids.move_line_ids.qty_done = 4.0
        l2.date_planned = self.d1
        self.assertEqual(self._picking_of(l2), picking)
        self.assertEqual(l2.move_ids.move_line_ids.qty_done, 4.0)

    def test_26_new_line_after_confirmation(self):
        self._configure(by_date=True, by_location=True)
        po = self._po([{'product': self.p1, 'date': self.d1, 'location': self.shelf_a}])
        existing = po.picking_ids
        po.write({'order_line': [
            (0, 0, {'product_id': self.p2.id, 'product_qty': 3, 'price_unit': 1,
                    'date_planned': self.d1, 'location_dest_id': self.shelf_a.id}),
            (0, 0, {'product_id': self.p3.id, 'product_qty': 2, 'price_unit': 1,
                    'date_planned': self.d2, 'location_dest_id': self.shelf_b.id}),
        ]})
        l2, l3 = po.order_line[1:]
        self.assertEqual(self._picking_of(l2), existing)
        self.assertNotEqual(self._picking_of(l3), existing)
        self.assertEqual(self._picking_of(l3).location_dest_id, self.shelf_b)
        self.assertEqual(self._picking_of(l3).split_grn_date, self._local_date(self.d2))
        self.assertEqual(len(po.picking_ids), 2)

    def test_27_quantity_changes_stay_in_line_receipt(self):
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d2}])
        l2 = po.order_line[1]
        picking = self._picking_of(l2)
        # as sent by the form view: quantity together with the unchanged date
        l2.write({'product_qty': 15, 'date_planned': l2.date_planned})
        self.assertEqual(len(po.picking_ids), 2)
        self.assertEqual(self._picking_of(l2), picking)
        self.assertEqual(sum(picking.move_ids.mapped('product_uom_qty')), 15)
        l2.write({'product_qty': 6, 'date_planned': l2.date_planned})
        self.assertEqual(len(po.picking_ids), 2)
        open_moves = picking.move_ids.filtered(lambda m: m.state != 'cancel')
        self.assertEqual(sum(open_moves.mapped('product_uom_qty')), 6)

    def test_27b_quantity_write_recomputing_date_stays_consistent(self):
        """A backend quantity write lets core recompute date_planned; the
        line's moves must still end up together in the matching receipt."""
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d2}])
        l2 = po.order_line[1]
        l2.product_qty = 15
        picking = self._picking_of(l2)
        self.assertEqual(picking.split_grn_date, po._split_grn_local_date(l2.date_planned))
        self.assertEqual(sum(l2.move_ids.mapped('product_uom_qty')), 15)
        self.assertEqual(len(po.picking_ids), 2)
        for receipt in po.picking_ids:
            self.assertTrue(receipt.move_ids)

    def test_28_cannot_decrease_below_received(self):
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}])
        picking = po.picking_ids
        picking.move_ids.quantity_done = 10
        picking.button_validate()
        with self.assertRaises(UserError):
            po.order_line.product_qty = 5

    def test_29_reconfirm_after_draft_does_not_duplicate(self):
        self._configure(by_date=True, by_location=True)
        po = self._po([
            {'product': self.p1, 'date': self.d1, 'location': self.shelf_a},
            {'product': self.p2, 'date': self.d2, 'location': self.shelf_b},
        ])
        before = po.picking_ids
        po._create_picking()  # idempotent: nothing left to procure
        self.assertEqual(po.picking_ids, before)
        self.assertEqual(sum(po.picking_ids.move_ids.mapped('product_uom_qty')), 20)

    def test_30_toggle_off_after_confirmation_keeps_standard_flow(self):
        self._configure(by_date=True)
        po = self._po([{'product': self.p1, 'date': self.d1}, {'product': self.p2, 'date': self.d2}])
        self.assertEqual(len(po.picking_ids), 2)
        self._configure(enabled=False)
        po.order_line[0].date_planned = self.d3
        self.assertEqual(len(po.picking_ids), 2, 'no rerouting when the feature is off')
        po.write({'order_line': [(0, 0, {'product_id': self.p3.id, 'product_qty': 1,
                                          'price_unit': 1, 'date_planned': self.d3})]})
        self.assertEqual(len(po.picking_ids), 2, 'standard flow adds the line to an existing receipt')

    def test_31_multi_step_reception(self):
        """Two-step reception: lines default to WH/Input and the chained
        Input → Stock transfer prevents silently re-routing the receipt."""
        self._configure(by_location=True)
        self.warehouse.reception_steps = 'two_steps'
        input_loc = self.warehouse.wh_input_stock_loc_id
        input_dock = self.env['stock.location'].create(
            {'name': 'Dock 2', 'location_id': input_loc.id, 'usage': 'internal'})
        po = self._po([{'product': self.p1}, {'product': self.p2, 'location': input_dock}])
        l1, l2 = po.order_line
        self.assertEqual(l1.location_dest_id, input_loc)
        self.assertEqual(len(po.picking_ids), 2)
        self.assertEqual(self._picking_of(l1).location_dest_id, input_loc)
        self.assertEqual(self._picking_of(l2).location_dest_id, input_dock)
        self.assertTrue(l1.move_ids.move_dest_ids, 'push rule chained the receipt to Input -> Stock')
        with self.assertRaises(UserError):
            l1.location_dest_id = self.shelf_a

    def test_32_lines_without_stockable_products(self):
        self._configure(by_date=True, by_location=True)
        po = self._po([{'product': self.service}])
        self.assertFalse(po.picking_ids)
        self.assertEqual(po.state, 'purchase')

    def test_33_display_lines_are_ignored(self):
        self._configure(by_date=True, by_location=True)
        po = self._po([{'product': self.p1}], confirm=False)
        po.write({'order_line': [(0, 0, {'display_type': 'line_section', 'name': 'Section', 'product_qty': 0})]})
        section = po.order_line.filtered('display_type')
        self.assertFalse(section.location_dest_id)
        po.button_confirm()
        self.assertEqual(len(po.picking_ids), 1)

    def test_34_reordering_rule_line_keeps_rule_sublocation(self):
        """Orderpoint lines default to the order destination; their moves
        still go to the rule's sub-location like in standard Odoo."""
        self._configure(by_date=True, by_location=True)
        self.p1.seller_ids = [(0, 0, {'partner_id': self.vendor.id, 'price': 3})]
        self.p1.route_ids = [(4, self.env.ref('purchase_stock.route_warehouse0_buy').id)]
        self.env['stock.warehouse.orderpoint'].create({
            'product_id': self.p1.id, 'location_id': self.shelf_a.id,
            'warehouse_id': self.warehouse.id, 'product_min_qty': 4, 'product_max_qty': 8})
        self.env['procurement.group'].run_scheduler()
        po = self.env['purchase.order'].search([('partner_id', '=', self.vendor.id)])
        self.assertEqual(len(po), 1)
        self.assertEqual(po.order_line.location_dest_id, self.stock)
        po.button_confirm()
        self.assertEqual(po.order_line.move_ids.location_dest_id, self.shelf_a)
        self.assertEqual(len(po.picking_ids), 1)

    # split by warehouse ------------------------------------------------------
    def _other_wh_shelf(self):
        return self.env['stock.location'].create(
            {'name': 'OWH Shelf', 'location_id': self.other_wh.lot_stock_id.id, 'usage': 'internal'})

    def _po_wh(self, lines, confirm=True):
        """Like _po, with an optional 'warehouse' per line."""
        po = self._po([dict(l, location=None) for l in lines], confirm=False)
        for line, spec in zip(po.order_line, lines):
            vals = {}
            if spec.get('warehouse'):
                vals['warehouse_id'] = spec['warehouse'].id
            if spec.get('location'):
                vals['location_dest_id'] = spec['location'].id
            if vals:
                line.write(vals)
        if confirm:
            po.button_confirm()
        return po

    def test_40_line_warehouse_defaults(self):
        self._configure(by_warehouse=True)
        po = self._po([{'product': self.p1}], confirm=False)
        line = po.order_line
        self.assertEqual(line.warehouse_id, self.warehouse)
        self.assertEqual(line.location_dest_id, self.stock)
        line.warehouse_id = self.other_wh
        self.assertEqual(line.location_dest_id, self.other_wh.in_type_id.default_location_dest_id,
                         'changing the warehouse moves the line to that warehouse\'s receipt location')

    def test_41_split_by_warehouse(self):
        self._configure(by_warehouse=True)
        po = self._po_wh([
            {'product': self.p1},
            {'product': self.p2, 'warehouse': self.other_wh},
            {'product': self.p3, 'warehouse': self.other_wh, 'date': self.d2},  # date not a criterion
        ])
        self.assertEqual(len(po.picking_ids), 2)
        l1, l2, l3 = po.order_line
        main, other = self._picking_of(l1), self._picking_of(l2)
        self.assertEqual(self._picking_of(l3), other)
        self.assertEqual(main.picking_type_id, self.warehouse.in_type_id)
        self.assertEqual(other.picking_type_id, self.other_wh.in_type_id)
        self.assertTrue(other.name.startswith('OWH/IN/'), other.name)
        self.assertEqual(other.location_dest_id, self.other_wh.lot_stock_id)
        self.assertEqual(other.move_ids.warehouse_id, self.other_wh)
        self.assertEqual(other.move_ids.picking_type_id, self.other_wh.in_type_id)
        self.assertEqual(main.move_ids.warehouse_id, self.warehouse)
        self.assertEqual(po.incoming_picking_count, 2)

    def test_42_warehouse_date_and_location_combined(self):
        self._configure(by_date=True, by_location=True, by_warehouse=True)
        owh_shelf = self._other_wh_shelf()
        po = self._po_wh([
            {'product': self.p1, 'date': self.d1, 'location': self.shelf_a},
            {'product': self.p2, 'date': self.d1, 'warehouse': self.other_wh, 'location': owh_shelf},
            {'product': self.p3, 'date': self.d1, 'warehouse': self.other_wh},
            {'product': self.p4, 'date': self.d2, 'warehouse': self.other_wh},
        ])
        self.assertEqual(len(po.picking_ids), 4)
        keys = {(p.picking_type_id.warehouse_id.code, p.location_dest_id.id, p.split_grn_date)
                for p in po.picking_ids}
        self.assertEqual(keys, {
            (self.warehouse.code, self.shelf_a.id, self._local_date(self.d1)),
            ('OWH', owh_shelf.id, self._local_date(self.d1)),
            ('OWH', self.other_wh.lot_stock_id.id, self._local_date(self.d1)),
            ('OWH', self.other_wh.lot_stock_id.id, self._local_date(self.d2)),
        })

    def test_43_location_must_belong_to_line_warehouse(self):
        self._configure(by_location=True, by_warehouse=True)
        po = self._po([{'product': self.p1}], confirm=False)
        with self.assertRaises(ValidationError):
            po.order_line.location_dest_id = self.other_wh.lot_stock_id
        po.order_line.write({'warehouse_id': self.other_wh.id,
                             'location_dest_id': self._other_wh_shelf().id})
        self.assertEqual(po.order_line.warehouse_id, self.other_wh)

    def test_44_receive_in_other_warehouse(self):
        self._configure(by_warehouse=True)
        po = self._po_wh([{'product': self.p1}, {'product': self.p2, 'warehouse': self.other_wh}])
        other = self._picking_of(po.order_line[1])
        other.move_ids.quantity_done = 10
        other.button_validate()
        self.assertEqual(other.state, 'done')
        self.assertEqual(po.order_line[1].qty_received, 10)
        self.assertEqual(po.order_line[0].qty_received, 0)
        Quant = self.env['stock.quant']
        self.assertEqual(Quant._get_available_quantity(self.p2, self.other_wh.lot_stock_id), 10)
        self.assertEqual(Quant._get_available_quantity(self.p2, self.stock), 0)

    def test_45_warehouse_change_after_confirmation(self):
        self._configure(by_warehouse=True)
        po = self._po_wh([{'product': self.p1}, {'product': self.p2}])
        shared = po.picking_ids
        self.assertEqual(len(shared), 1)
        l2 = po.order_line[1]
        l2.warehouse_id = self.other_wh
        self.assertEqual(len(po.picking_ids), 2)
        moved = self._picking_of(l2)
        self.assertEqual(moved.picking_type_id, self.other_wh.in_type_id)
        self.assertEqual(l2.move_ids.warehouse_id, self.other_wh)
        self.assertEqual(l2.move_ids.location_dest_id, self.other_wh.lot_stock_id)
        self.assertEqual(l2.move_ids.state, 'assigned')
        # back again: merges into the main receipt; the other-warehouse one is removed,
        # never re-keyed across operation types
        l2.warehouse_id = self.warehouse
        self.assertFalse(moved.exists())
        self.assertEqual(po.picking_ids, shared)
        self.assertEqual(l2.move_ids.picking_type_id, self.warehouse.in_type_id)

    def test_46_sole_receipt_not_rekeyed_across_warehouses(self):
        self._configure(by_warehouse=True)
        po = self._po_wh([{'product': self.p1}])
        original = po.picking_ids
        po.order_line.warehouse_id = self.other_wh
        self.assertFalse(original.exists())
        self.assertTrue(po.picking_ids.name.startswith('OWH/IN/'))

    def test_47_warehouse_criterion_off_keeps_order_warehouse(self):
        self._configure(by_location=True)  # warehouse split off
        po = self._po_wh([{'product': self.p1, 'warehouse': self.other_wh}])
        self.assertEqual(po.picking_ids.picking_type_id, self.warehouse.in_type_id)
        self.assertEqual(po.picking_ids.location_dest_id, self.stock)

    def test_48_two_step_reception_in_other_warehouse(self):
        self._configure(by_warehouse=True)
        self.other_wh.reception_steps = 'two_steps'
        po = self._po_wh([{'product': self.p1, 'warehouse': self.other_wh}])
        receipt = po.picking_ids
        self.assertEqual(receipt.location_dest_id, self.other_wh.wh_input_stock_loc_id)
        internal = receipt.move_ids.move_dest_ids.picking_id
        self.assertEqual(internal.picking_type_id, self.other_wh.int_type_id,
                         'the other warehouse\'s own push rule handles Input -> Stock')
        with self.assertRaises(UserError):
            po.order_line.warehouse_id = self.warehouse

    def test_49_warehouse_setting_persists(self):
        settings = self.env['res.config.settings'].create({
            'split_grn_enabled': True, 'split_grn_by_warehouse': True})
        settings.execute()
        self.assertTrue(self.company.split_grn_by_warehouse)
        po = self._po([{'product': self.p1}], confirm=False)
        self.assertTrue(po.split_grn_warehouse_active)
        self.assertTrue(po.split_grn_active)

    def test_50_changing_order_warehouse_moves_lines(self):
        """Changing the order's Deliver To recomputes line warehouse and
        location together, without tripping the warehouse constraint."""
        self._configure(by_location=True, by_warehouse=True)
        po = self._po([{'product': self.p1}, {'product': self.p2}], confirm=False)
        po.picking_type_id = self.other_wh.in_type_id
        self.assertEqual(po.order_line.warehouse_id, self.other_wh)
        self.assertEqual(po.order_line.location_dest_id, self.other_wh.lot_stock_id)
        po.button_confirm()
        self.assertEqual(po.picking_ids.picking_type_id, self.other_wh.in_type_id)
