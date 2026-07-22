import sys
import odoo
odoo.tools.config.parse_config(['-c', '/opt/odoo19/odoo.conf'])
env = odoo.api.Environment(odoo.registry(odoo.tools.config['db_name']).cursor(), 1, {})
html = env['ir.ui.view']._render_template('portal.portal_docs_entry', {'title': 'My Shipments', 'url': '/my/shipments', 'count': 0, 'icon': '/keralariders_logistics/static/src/img/shipment.svg', 'text': 'Manage your deliveries'})
print(html)
