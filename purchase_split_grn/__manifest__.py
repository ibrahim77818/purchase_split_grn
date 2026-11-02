# -*- coding: utf-8 -*-
{
    'name': 'Purchase Split Receipts (GRN) by Date, Location & Warehouse',
    'version': '18.0.1.0.0',
    'category': 'Inventory/Purchase',
    'summary': 'Automatically split purchase receipts by expected arrival date, '
               'destination location and/or warehouse per purchase order line',
    'description': """
Split Goods Receipt Notes (GRN)
===============================
* Company-level switch in Purchase Settings (Inbound Logistics section).
* Split incoming shipments by expected arrival date, by destination
  location, by warehouse, or any combination.
* Destination location per purchase order line (defaults to the
  order's receipt location, overridable per line).
* Receiving warehouse per purchase order line: lines received in another
  warehouse of the same company get their own receipt, created with that
  warehouse's receipt operation, sequence and routes.
* Keeps receipts consistent when line dates or locations change after
  confirmation: moves are re-routed and empty receipts are removed.
* Arabic (ar_001) translation and RTL-friendly views.
""",
    'author': 'Ibrahim Yahia',
    'website': 'https://www.linkedin.com/in/ibrahim-yahia',
    'license': 'LGPL-3',
    'depends': ['purchase_stock'],
    'data': [
        'security/purchase_split_grn_security.xml',
        'views/res_config_settings_views.xml',
        'views/purchase_order_views.xml',
        'views/stock_picking_views.xml',
    ],
    'images': ['static/description/banner.png'],
    'installable': True,
    'application': False,
    'auto_install': False,
}
