"""100×150 mm KeralaXpress shipping label and paper-size picker."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

ARTICLE = 'EY547878418IN'
SELLER_STREET = 'Managath House Pickup Lane'


@tagged('post_install', '-at_install')
class TestAwbPaperSize(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Label IP Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': SELLER_STREET,
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.hub_seller = cls.env['logistics.seller'].create({
            'name': 'Label Hub Seller',
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
            'shipping_to_name': 'Label Customer',
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

    def _label_html(self, shipment):
        html = self.env['ir.actions.report']._render_qweb_html(
            'keralariders_logistics.report_shipment_document_100x150',
            shipment.ids,
        )[0]
        if isinstance(html, bytes):
            html = html.decode('utf-8')
        return html

    def test_paper_param_selects_100x150_report(self):
        Shipment = self.env['logistics.shipment']
        self.assertEqual(
            Shipment._awb_report_xmlid_for_paper('100x150'),
            'keralariders_logistics.action_report_shipment_100x150',
        )
        self.assertEqual(
            Shipment._awb_report_xmlid_for_paper('100 × 150'),
            'keralariders_logistics.action_report_shipment_100x150',
        )
        self.assertEqual(
            Shipment._awb_report_xmlid_for_paper('a4'),
            'keralariders_logistics.action_report_shipment',
        )
        self.assertEqual(
            Shipment._awb_report_xmlid_for_paper(None),
            'keralariders_logistics.action_report_shipment',
        )
        self.assertEqual(
            Shipment._awb_report_xmlid_for_paper('letter'),
            'keralariders_logistics.action_report_shipment',
        )

    def test_paperformat_is_exactly_100x150(self):
        paper = self.env.ref(
            'keralariders_logistics.paperformat_shipping_label_100x150')
        self.assertEqual(paper.format, 'custom')
        self.assertEqual(float(paper.page_width), 100.0)
        self.assertEqual(float(paper.page_height), 150.0)
        self.assertEqual(paper.orientation, 'Portrait')
        report = self.env.ref(
            'keralariders_logistics.action_report_shipment_100x150')
        self.assertEqual(report.paperformat_id, paper)
        self.assertEqual(
            report.report_name,
            'keralariders_logistics.report_shipment_document_100x150',
        )

    def test_backend_wizard_prints_matching_report(self):
        shipment = self._new_shipment(self.hub_seller)
        action = shipment.action_print_awb_picker()
        self.assertEqual(action['type'], 'ir.actions.act_window')
        self.assertEqual(action['res_model'], 'logistics.awb.print.wizard')
        self.assertEqual(action['target'], 'new')
        wizard = self.env['logistics.awb.print.wizard'].browse(action['res_id'])
        self.assertEqual(wizard.shipment_ids, shipment)
        self.assertEqual(wizard.paper_size, 'a4')

        a4 = wizard.action_print()
        self.assertEqual(a4['type'], 'ir.actions.report')
        self.assertEqual(
            a4['report_name'],
            'keralariders_logistics.report_shipment_document',
        )

        wizard.paper_size = '100x150'
        thermal = wizard.action_print()
        self.assertEqual(
            thermal['report_name'],
            'keralariders_logistics.report_shipment_document_100x150',
        )

        order_action = shipment.order_id.action_print_awb_delivery_slips()
        self.assertEqual(order_action['res_model'], 'logistics.awb.print.wizard')
        order_wizard = self.env['logistics.awb.print.wizard'].browse(
            order_action['res_id'])
        self.assertEqual(order_wizard.shipment_ids, shipment)

    def test_backend_wizard_view_exists(self):
        view = self.env.ref(
            'keralariders_logistics.view_awb_print_wizard_form')
        self.assertEqual(view.model, 'logistics.awb.print.wizard')
        self.assertIn('paper_size', view.arch_db)

    def test_empty_order_print_raises(self):
        order = self.env['logistics.order'].create({
            'seller_id': self.hub_seller.id,
        })
        with self.assertRaises(UserError):
            order.action_print_awb_delivery_slips()

    def test_hub_100x150_has_kx_marks_not_indiapost(self):
        shipment = self._new_shipment(self.hub_seller)
        html = self._label_html(shipment)
        self.assertIn('kx-label-100x150', html)
        self.assertIn(shipment.name, html)
        self.assertIn('Hub Pickup Street', html)
        self.assertIn('Label Customer', html)
        self.assertIn('9876543210', html)
        self.assertIn('PREPAID', html)
        self.assertIn('KERALAXPRESS', html)
        self.assertIn('kx-label-qr', html)
        self.assertIn('bcid=code128', html)
        self.assertIn('kx-label-kx-logo', html)
        self.assertNotIn('awb-indiapost-logo', html)
        self.assertNotIn('alt="India Post"', html)
        self.assertNotIn(ARTICLE, html)

    def test_indiapost_100x150_includes_both_logos_and_arn(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
            'indiapost_sort_code': 'S',
        })
        html = self._label_html(shipment)
        self.assertIn('kx-label-100x150', html)
        self.assertIn('awb-indiapost-logo', html)
        self.assertIn('alt="India Post"', html)
        self.assertIn('kx-label-kx-logo', html)
        self.assertIn(ARTICLE, html)
        self.assertIn('awb-indiapost-barcode', html)
        self.assertIn(SELLER_STREET, html)
        self.assertIn('Label Customer', html)
        self.assertIn('SPEED POST', html)
        self.assertIn(shipment.name, html)
        self.assertIn('AWB', html)

    def test_label_xml_parses(self):
        layout = (
            Path(__file__).resolve().parents[1]
            / 'report' / 'shipment_label_100x150.xml'
        )
        tree = ET.parse(layout)
        ids = {
            el.attrib.get('id')
            for el in tree.iter()
            if el.attrib.get('id')
        }
        self.assertIn('paperformat_shipping_label_100x150', ids)
        self.assertIn('report_shipment_document_100x150', ids)
        self.assertIn('action_report_shipment_100x150', ids)
        source = layout.read_text(encoding='utf-8')
        self.assertIn('page_width">100<', source)
        self.assertIn('page_height">150<', source)


@tagged('post_install', '-at_install')
class TestAwbPaperSizePortal(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Label Portal Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'own_network'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.portal_login = 'kx_label_paper_portal'
        cls.portal_user = cls.env['res.users'].create({
            'name': 'Label Portal',
            'login': cls.portal_login,
            'password': cls.portal_login,
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def setUp(self):
        super().setUp()
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        self.wallet.invalidate_recordset(['balance'])

    def _csrf(self, html):
        match = re.search(
            r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html)
        self.assertTrue(match, 'no csrf_token in the rendered page')
        return match.group(1)

    def _hidden_value(self, html, name):
        match = re.search(
            r'name="%s"[^>]*\bvalue="([^"]*)"' % re.escape(name), html)
        self.assertTrue(match, 'no %s in the rendered page' % name)
        return match.group(1)

    def _print_redirect(self, path):
        return self.url_open(path, allow_redirects=False)

    def _create_printable_order(self):
        self.authenticate(self.portal_login, self.portal_login)
        form = self.url_open('/my/orders/manual')
        self.url_open('/my/orders/create', data={
            'csrf_token': self._csrf(form.text),
            'shipping_to_name': 'Web Order Customer',
            'shipping_to_mobile': '9876543210',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'item_description': 'Toys',
            'total_weight': '0.5',
            'order_payment_type': 'prepaid',
            'pickup_date': fields.Date.context_today(self.env.user).isoformat(),
        })
        self.env.invalidate_all()
        order = self.env['logistics.order'].search(
            [('seller_id', '=', self.seller.id)], order='id desc', limit=1)
        self.assertTrue(order)
        detail = self.url_open('/my/orders/%s' % order.id)
        self.url_open('/my/orders/request_pickup', data={
            'csrf_token': self._csrf(detail.text),
            'order_id': str(order.id),
            'pickup_confirm_token': self._hidden_value(
                detail.text, 'pickup_confirm_token'),
        })
        self.env.invalidate_all()
        return order

    def test_portal_modal_present_on_print_pages(self):
        order = self._create_printable_order()
        shipment = order.shipment_ids
        listing = self.url_open('/my/orders')
        self.assertIn('kxPrintLabelModal', listing.text)
        self.assertIn('Print shipping labels', listing.text)
        self.assertIn('100 × 150 mm', listing.text)
        self.assertIn('kx-print-awb', listing.text)
        self.assertIn('/my/orders/%s/print' % order.id, listing.text)

        detail = self.url_open('/my/orders/%s' % order.id)
        self.assertIn('kxPrintLabelModal', detail.text)
        self.assertIn('Print AWBs', detail.text)

        shipments = self.url_open('/my/shipments')
        self.assertIn('kxPrintLabelModal', shipments.text)
        self.assertIn('/my/shipments/%s/print' % shipment.id, shipments.text)

    def test_portal_paper_param_selects_100x150_report(self):
        order = self._create_printable_order()
        shipment = order.shipment_ids[0]
        thermal = self._print_redirect(
            '/my/orders/%s/print?paper=100x150' % order.id)
        self.assertIn(thermal.status_code, (301, 302, 303, 307))
        self.assertIn(
            '/report/pdf/keralariders_logistics.action_report_shipment_100x150',
            thermal.headers.get('Location', ''),
        )

        a4 = self._print_redirect('/my/orders/%s/print?paper=a4' % order.id)
        self.assertIn(
            '/report/pdf/keralariders_logistics.action_report_shipment/',
            a4.headers.get('Location', ''),
        )
        self.assertNotIn('100x150', a4.headers.get('Location', ''))

        default = self._print_redirect('/my/orders/%s/print' % order.id)
        self.assertIn(
            '/report/pdf/keralariders_logistics.action_report_shipment/',
            default.headers.get('Location', ''),
        )
        self.assertNotIn('100x150', default.headers.get('Location', ''))

        ship_thermal = self._print_redirect(
            '/my/shipments/%s/print?paper=100x150' % shipment.id)
        self.assertIn(
            '/report/pdf/keralariders_logistics.action_report_shipment_100x150/%s'
            % shipment.id,
            ship_thermal.headers.get('Location', ''),
        )

    def test_draft_still_forbidden_for_100x150(self):
        self.authenticate(self.portal_login, self.portal_login)
        form = self.url_open('/my/orders/manual')
        self.url_open('/my/orders/create', data={
            'csrf_token': self._csrf(form.text),
            'shipping_to_name': 'Draft Customer',
            'shipping_to_mobile': '9876543210',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'item_description': 'Toys',
            'total_weight': '0.5',
            'order_payment_type': 'prepaid',
            'pickup_date': fields.Date.context_today(self.env.user).isoformat(),
        })
        self.env.invalidate_all()
        order = self.env['logistics.order'].search(
            [('seller_id', '=', self.seller.id)], order='id desc', limit=1)
        shipment = order.shipment_ids
        self.assertFalse(order.portal_awb_printable())

        denied = self._print_redirect(
            '/my/orders/%s/print?paper=100x150' % order.id)
        self.assertIn(denied.status_code, (301, 302, 303, 307))
        self.assertNotIn('/report/pdf', denied.headers.get('Location', ''))

        ship_denied = self._print_redirect(
            '/my/shipments/%s/print?paper=100x150' % shipment.id)
        self.assertNotIn('/report/pdf', ship_denied.headers.get('Location', ''))

        listing = self.url_open('/my/orders')
        self.assertNotIn('/my/orders/%s/print' % order.id, listing.text)

        with self.assertRaises(AccessError):
            self.env['ir.actions.report'].with_user(self.portal_user)._render_qweb_html(
                'keralariders_logistics.report_shipment_document_100x150',
                shipment.ids,
            )
