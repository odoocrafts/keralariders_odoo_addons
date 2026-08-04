{
    'name': 'Kerala Riders Logistics',
    'version': '1.1',
    'category': 'Operations/Logistics',
    'summary': 'Last-mile delivery management platform for Kerala Riders',
    'description': """
Kerala Riders Logistics Management
====================================
A web-based logistics management platform for Kerala Riders to manage
last-mile delivery operations for vendors across Kerala.

Features:
- Seller/Vendor management with wallet system
- Bulk shipment upload via Excel
- Delivery executive management
- Hub custody and district hub network (14 Kerala hubs)
- Real-time shipment tracking with public tracking URL
- COD tracking and management
- Seller / DE / Hub Manager portals
- Comprehensive reporting
    """,
    'author': 'Odoocrafts',
    'website': 'https://keralariders.com',
    'depends': ['base', 'mail', 'contacts', 'portal'],
    'data': [
        'security/groups.xml',
        'security/security.xml',
        'security/ir.model.access.csv',
        'data/districts.xml',
        'data/logistics.pincode.csv',
        'data/hubs.xml',
        'data/hub_pincode_assign.xml',
        'data/sequence.xml',
        'data/delivery_charges.xml',
        'views/seller_views.xml',
        'views/wallet_views.xml',
        'views/wallet_recharge_views.xml',
        'views/delivery_executive_views.xml',
        'views/shipment_views.xml',
        'views/delivery_package_views.xml',
        'views/delivery_charges_views.xml',
        'views/order_views.xml',
        "views/account_views.xml",
        "wizard/cod_payment_views.xml",
        "wizard/create_hub_manager_wizard_views.xml",
        "views/hub_views.xml",
        'views/res_config_settings_views.xml',
        'views/tracking_template.xml',
        'views/portal_order_templates.xml',
        'views/portal_templates.xml',
        'views/wizard_assign_delivery_executive.xml',
        'views/portal_delivery_templates.xml',
        'views/portal_hub_templates.xml',
        'views/brand_overrides.xml',
        'report/shipment_layout.xml',
    ],
    'post_init_hook': '_post_init_assign_hub_pincodes',
    'images': ['static/description/icon.png'],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
