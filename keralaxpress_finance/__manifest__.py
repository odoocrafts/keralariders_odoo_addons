{
    'name': 'KeralaXpress Reports & Accounts',
    'version': '19.0.1.2.1',
    'category': 'Operations/Logistics',
    'summary': 'Order and COD reports, seller ledger, monthly seller invoices '
               'and company expenses for KeralaXpress',
    'description': """
KeralaXpress Reports & Accounts
===============================
Built on top of Kerala Riders Logistics without changing how money moves.

Reports
- Order reports per shipment: count, value, delivered, cancelled, status
  summary and seller-wise statistics, filtered by date, seller and status.
- COD wallet: total balance, seller-wise balance and a date-wise report.

Accounts
- Seller ledger: wallet and COD lines with running balances, statement
  export to PDF and Excel.
- Monthly seller invoices on services used, GST included in the amount.
- Company expense management with approval.

Everything a seller is allowed to see is also in the seller portal, and
every report exports to Excel.
    """,
    'author': 'Odoocrafts',
    'website': 'https://keralariders.com',
    'depends': ['keralariders_logistics'],
    'external_dependencies': {'python': ['xlsxwriter']},
    'data': [
        'security/ir.model.access.csv',
        'security/security.xml',
        'data/sequence.xml',
        'data/expense_category_data.xml',
        'data/cron.xml',
        'report/report_layout.xml',
        'report/seller_invoice_report.xml',
        'report/ledger_statement_report.xml',
        'views/order_report_views.xml',
        'views/cod_ledger_views.xml',
        'views/seller_ledger_views.xml',
        'views/seller_invoice_views.xml',
        'views/expense_views.xml',
        'views/res_config_settings_views.xml',
        'wizard/order_report_wizard_views.xml',
        'wizard/cod_report_wizard_views.xml',
        'wizard/ledger_statement_wizard_views.xml',
        'wizard/invoice_generate_wizard_views.xml',
        'wizard/expense_report_wizard_views.xml',
        'views/menus.xml',
        'views/portal_templates.xml',
    ],
    'installable': True,
    'application': False,
    'license': 'LGPL-3',
}
