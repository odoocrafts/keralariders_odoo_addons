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
    'depends': ['base', 'mail', 'contacts', 'portal', 'website'],
    'data': [
        "security/groups.xml",
        "security/ir.model.access.csv",
        "data/districts.xml",
        "data/logistics.pincode.csv",
        "views/seller_views.xml",
        "views/wallet_views.xml",
        "views/delivery_executive_views.xml",
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
