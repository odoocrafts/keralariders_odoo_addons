"""Print AWB layout follows fulfilment_method; hub stays the old waybill.

Hermetic: no India Post HTTP, no barcode allocation, no live AWB consumption.
"""
import re
from urllib.parse import quote

from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

ARTICLE = 'EY547878418IN'
SELLER_STREET = 'Managath House Pickup Lane'
CONSIGNOR = 'KERALA XPRESS LOGISTICS'


@tagged('post_install', '-at_install')
class TestAwbPrintLayout(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'AWB IP Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': SELLER_STREET,
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.hub_seller = cls.env['logistics.seller'].create({
            'name': 'AWB Hub Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Hub Pickup Street',
        })
        cls.hub_seller.write({'fulfilment_method': 'own_network'})

    def _new_shipment(self, seller, **overrides):
        order = self.env['logistics.order'].create({'seller_id': seller.id})
        vals = {
            'order_id': order.id,
            'seller_id': seller.id,
            'shipping_to_name': 'AWB Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        }
        vals.update(overrides)
        return self.env['logistics.shipment'].create(vals)

    def _awb_html(self, shipment):
        html = self.env['ir.actions.report']._render_qweb_html(
            'keralariders_logistics.report_shipment_document',
            shipment.ids,
        )[0]
        if isinstance(html, bytes):
            html = html.decode('utf-8')
        return html

    def _seller_cell(self, html):
        match = re.search(
            r'class="awb-seller"[^>]*>(.*?)</td>', html, flags=re.DOTALL)
        self.assertTrue(match, 'India Post AWB is missing the seller cell')
        return match.group(1)

    def test_hub_print_awb_keeps_old_layout(self):
        shipment = self._new_shipment(self.hub_seller)
        self.assertEqual(shipment.fulfilment_method, 'own_network')
        html = self._awb_html(shipment)
        self.assertIn(shipment.name, html)
        self.assertIn('Hub Pickup Street', html)
        self.assertIn('AWB Hub Seller', html)
        self.assertNotIn('awb-layout-indiapost', html)
        self.assertNotIn('awb-indiapost-logo', html)
        self.assertNotIn('awb-indiapost-barcode', html)
        self.assertNotIn('TrackConsignment.aspx', html)
        self.assertNotIn('India Post', html)

    def test_indiapost_print_awb_uses_seller_and_ip_marks(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
        })
        html = self._awb_html(shipment)
        self.assertIn('awb-layout-indiapost', html)
        self.assertIn('awb-indiapost-logo', html)
        self.assertIn('awb-indiapost-barcode', html)
        self.assertIn(ARTICLE, html)
        self.assertIn(shipment.name, html)

        seller_html = self._seller_cell(html)
        self.assertIn(SELLER_STREET, seller_html)
        self.assertIn('AWB IP Seller', seller_html)
        self.assertNotIn(CONSIGNOR, seller_html)
        company_name = (shipment.company_id.name or '').strip()
        if company_name and company_name.upper() != 'AWB IP SELLER':
            self.assertNotIn(company_name, seller_html)

        payload = shipment._awb_indiapost_qr_payload()
        self.assertIn('TrackConsignment.aspx', payload)
        self.assertIn(ARTICLE, payload)
        self.assertIn(payload, html)
        self.assertIn(quote(payload, safe=''), html)
        self.assertIn(CONSIGNOR, self.env['logistics.indiapost.client']._ip_settings()[
            'indiapost_sender_name'])

    def test_awb_seller_address_never_uses_company(self):
        shipment = self._new_shipment(self.ip_seller)
        addr = shipment._awb_seller_address()
        self.assertEqual(addr['street'], SELLER_STREET)
        self.assertEqual(addr['name'], 'AWB IP Seller')
        self.assertNotEqual(addr['name'], shipment.company_id.name)
        self.assertNotEqual(addr['name'], CONSIGNOR)
        self.assertNotIn('Vazhiyambalam', addr['street'])


@tagged('post_install', '-at_install')
class TestAwbPrintLayoutPortal(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'AWB Portal IP Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': SELLER_STREET,
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.ip_login = 'kx_awb_layout_portal_ip'
        cls.env['res.users'].create({
            'name': 'AWB Portal IP',
            'login': cls.ip_login,
            'password': cls.ip_login,
            'partner_id': cls.ip_seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def test_portal_print_awb_renders_indiapost_layout(self):
        order = self.env['logistics.order'].create({'seller_id': self.ip_seller.id})
        shipment = self.env['logistics.shipment'].create({
            'order_id': order.id,
            'seller_id': self.ip_seller.id,
            'shipping_to_name': 'Portal AWB Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Toys',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        })
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
        })
        html = self.env['ir.actions.report']._render_qweb_html(
            'keralariders_logistics.report_shipment_document',
            shipment.ids,
        )[0]
        if isinstance(html, bytes):
            html = html.decode('utf-8')
        self.assertIn('awb-layout-indiapost', html)
        self.assertIn(SELLER_STREET, html)
        self.assertIn(ARTICLE, html)

        self.authenticate(self.ip_login, self.ip_login)
        response = self.url_open(
            '/report/html/keralariders_logistics.action_report_shipment/%s'
            % shipment.id)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8', errors='replace')
        self.assertIn('awb-layout-indiapost', body)
        self.assertIn(SELLER_STREET, body)
        self.assertIn(ARTICLE, body)
