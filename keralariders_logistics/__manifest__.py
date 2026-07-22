{
    'name': 'Kerala Riders Logistics',
    'version': '1.0',
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
- Real-time shipment tracking with public tracking URL
- COD tracking and management
- Seller portal for self-service
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
        'data/sequence.xml',
        'data/delivery_charges.xml',
        'views/seller_views.xml',
        'views/wallet_views.xml',
        'views/wallet_recharge_views.xml',
        'views/delivery_executive_views.xml',
        'views/shipment_views.xml',
        'views/delivery_charges_views.xml',
        'views/tracking_template.xml',
        'views/portal_templates.xml',
        'report/shipment_layout.xml',
    ],
    'images': ['static/description/icon.png'],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
