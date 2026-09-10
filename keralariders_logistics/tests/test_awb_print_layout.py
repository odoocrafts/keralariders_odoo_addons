"""Print AWB layout follows fulfilment_method; hub stays the old waybill.

Hermetic: no India Post HTTP, no barcode allocation, no live AWB consumption.
India Post Print AWB is one page: no KeralaXpress AWB barcode/QR, no CEPT merge.
"""
import base64
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
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

    def _shipto_cell(self, html):
        match = re.search(
            r'class="awb-shipto"[^>]*>(.*?)</td>', html, flags=re.DOTALL)
        self.assertTrue(match, 'India Post AWB is missing the ship-to cell')
        return match.group(1)

    def _sort_cell(self, html):
        match = re.search(
            r'class="awb-indiapost-sort"[^>]*>(.*?)</td>', html, flags=re.DOTALL)
        self.assertTrue(match, 'India Post AWB is missing the sort/PIN cell')
        return match.group(1)

    def _header_ip_cell(self, html):
        match = re.search(
            r'class="awb-header-indiapost"[^>]*>(.*?)</td>', html,
            flags=re.DOTALL)
        self.assertTrue(match, 'India Post AWB is missing the carrier logo cell')
        return match.group(1)

    def test_hub_print_awb_keeps_old_layout(self):
        shipment = self._new_shipment(self.hub_seller)
        self.assertEqual(shipment.fulfilment_method, 'own_network')
        html = self._awb_html(shipment)
        self.assertIn(shipment.name, html)
        self.assertIn('Hub Pickup Street', html)
        self.assertIn('AWB Hub Seller', html)
        self.assertIn('AWB#', html)
        self.assertIn('bcid=code128', html)
        self.assertIn('create-qr-code', html)
        self.assertNotIn('awb-layout-indiapost', html)
        self.assertNotIn('awb-indiapost-logo', html)
        self.assertNotIn('awb-indiapost-barcode', html)
        self.assertNotIn('awb-header-indiapost', html)
        self.assertNotIn('TrackConsignment.aspx', html)
        self.assertNotIn('India Post', html)
        self.assertNotIn('Mob:', html)
        self.assertNotIn('Phone:', html)

    def test_indiapost_print_awb_uses_seller_and_ip_marks(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
            'indiapost_sort_code': 'S',
        })
        html = self._awb_html(shipment)
        self.assertIn('awb-layout-indiapost', html)
        self.assertIn('awb-indiapost-logo', html)
        self.assertIn('awb-indiapost-barcode', html)
        self.assertIn(ARTICLE, html)
        self.assertIn('data:image/png;base64,', html)

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

        self.assertNotIn('AWB#', html)
        self.assertNotIn('bcid=code128&text=%s' % shipment.name, html)
        self.assertNotIn('bcid=code128&amp;text=%s' % shipment.name, html)
        self.assertNotIn('data=%s' % shipment.name, html)
        self.assertNotRegex(html, r'>PIN<')
        header_ip = self._header_ip_cell(html)
        self.assertIn('awb-indiapost-logo', header_ip)
        self.assertIn('alt="India Post"', header_ip)

    def test_indiapost_sort_letter_used_when_set(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
            'indiapost_sort_code': 'A',
        })
        self.assertEqual(shipment._awb_indiapost_sort_code(), 'A')
        html = self._awb_html(shipment)
        sort_html = self._sort_cell(html)
        self.assertIn('awb-indiapost-sort-letter', sort_html)
        self.assertIn('A', sort_html)
        self.assertIn('695001', sort_html)
        self.assertNotIn('PIN', sort_html)

    def test_indiapost_sort_box_omits_letter_when_unknown(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
        })
        self.assertEqual(shipment._awb_indiapost_sort_code(), '')
        html = self._awb_html(shipment)
        sort_html = self._sort_cell(html)
        self.assertNotIn('awb-indiapost-sort-letter', sort_html)
        self.assertNotIn('PIN', sort_html)
        self.assertIn('695001', sort_html)

    def test_sort_code_parsed_from_stored_label_pdf(self):
        shipment = self._new_shipment(self.ip_seller)
        pdf = self._ip_pdf_with_text('S 680561')
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_label_pdf': base64.b64encode(pdf),
        })
        self.assertEqual(ipc.parse_label_sort_code('S 680561'), 'S')
        self.assertEqual(ipc.parse_label_sort_code('A New Delhi GPO\n110001'), 'A')
        self.assertEqual(ipc.parse_label_sort_code(''), '')
        self.assertEqual(ipc.parse_label_sort_code('RECEIVER: SABITHA'), '')
        self.assertEqual(ipc.parse_label_sort_code_from_pdf(pdf), 'S')
        self.assertEqual(shipment._awb_indiapost_sort_code(), 'S')

    def test_sort_code_persisted_from_label_not_hardcoded(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
        })
        air_pdf = self._ip_pdf_with_text('A 695001')
        code = shipment._ip_sort_code_from_label(air_pdf, 'S')
        self.assertEqual(code, 'A')
        blank = self._ip_blank_pdf_bytes()
        self.assertEqual(shipment._ip_sort_code_from_label(blank, 'S'), 'S')
        self.assertFalse(shipment._ip_sort_code_from_label(blank, ''))
        self.assertFalse(shipment._ip_sort_code_from_label(b'', None))

    def test_awb_seller_address_never_uses_company(self):
        shipment = self._new_shipment(self.ip_seller)
        addr = shipment._awb_seller_address()
        self.assertEqual(addr['street'], SELLER_STREET)
        self.assertEqual(addr['name'], 'AWB IP Seller')
        self.assertEqual(addr['phone'], '9400662693')
        self.assertNotEqual(addr['name'], shipment.company_id.name)
        self.assertNotEqual(addr['name'], CONSIGNOR)
        self.assertNotIn('Vazhiyambalam', addr['street'])

    def test_indiapost_print_awb_includes_customer_and_seller_phones(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
            'indiapost_sort_code': 'S',
        })
        self.assertEqual(shipment._awb_customer_phone(), '9876543210')
        self.assertEqual(shipment._awb_seller_phone(), '9400662693')
        html = self._awb_html(shipment)
        shipto_html = self._shipto_cell(html)
        seller_html = self._seller_cell(html)
        self.assertIn('Mob:', shipto_html)
        self.assertIn('9876543210', shipto_html)
        self.assertNotIn('9400662693', shipto_html)
        self.assertIn('Phone:', seller_html)
        self.assertIn('9400662693', seller_html)
        self.assertNotIn('9876543210', seller_html)

    def test_indiapost_print_awb_omits_empty_phone_lines(self):
        self.ip_seller.phone = False
        if self.ip_seller.partner_id:
            self.ip_seller.partner_id.write({'phone': False})
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
        })
        self.assertFalse(shipment._awb_seller_phone())
        html = self._awb_html(shipment)
        seller_html = self._seller_cell(html)
        shipto_html = self._shipto_cell(html)
        self.assertNotIn('Phone:', seller_html)
        self.assertIn('Mob:', shipto_html)
        self.assertIn('9876543210', shipto_html)

    def test_indiapost_awb_template_xml_parses(self):
        layout = Path(__file__).resolve().parents[1] / 'report' / 'shipment_layout.xml'
        tree = ET.parse(layout)
        ids = {
            el.attrib.get('id')
            for el in tree.iter()
            if el.attrib.get('id')
        }
        self.assertIn('report_shipment_document_indiapost', ids)
        source = layout.read_text(encoding='utf-8')
        self.assertIn("o._awb_customer_phone()", source)
        self.assertIn("seller_addr['phone']", source)
        self.assertIn('Mob:', source)
        self.assertIn('Phone:', source)

    def test_indiapost_logo_is_png_wordmark_not_emblem_svg(self):
        shipment = self._new_shipment(self.ip_seller)
        uri = shipment._awb_indiapost_logo_data_uri()
        self.assertTrue(uri.startswith('data:image/png;base64,'))
        self.assertGreater(len(uri), 1000)


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
            'indiapost_sort_code': 'S',
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
        self.assertNotIn('AWB#', html)
        self.assertNotRegex(html, r'>PIN<')

        self.authenticate(self.ip_login, self.ip_login)
        response = self.url_open(
            '/report/html/keralariders_logistics.action_report_shipment/%s'
            % shipment.id)
        self.assertEqual(response.status_code, 200)
        body = response.content.decode('utf-8', errors='replace')
        self.assertIn('awb-layout-indiapost', body)
        self.assertIn(SELLER_STREET, body)
        self.assertIn(ARTICLE, body)
        self.assertNotIn('AWB#', body)
