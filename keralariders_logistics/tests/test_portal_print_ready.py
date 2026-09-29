"""Seller portal Print waits for the India Post ARN; form defaults.

India Post booking runs after Request Pickup in the background. Until the
article number is stored the 100x150 label would carry the KeralaXpress AWB
barcode, so Print stays a disabled spinner and the PDF routes refuse.

Hermetic: no India Post HTTP. The article number is written the way booking
stores it.
"""

import re

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

ARTICLE = 'EA123456789IN'


@tagged('post_install', '-at_install')
class TestPortalPrintReady(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.env.company.country_id = cls.env.ref('base.in')
        cls.kerala = cls.env.ref('base.state_in_kl')
        cls.tamil_nadu = cls.env.ref('base.state_in_tn')
        portal_group = cls.env.ref('base.group_portal')

        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Print Ready IP Seller',
            'zip': '682001',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.ip_login = 'kx_print_ready_ip'
        cls.ip_user = cls.env['res.users'].create({
            'name': 'Print Ready IP',
            'login': cls.ip_login,
            'password': cls.ip_login,
            'partner_id': cls.ip_seller.partner_id.id,
            'group_ids': [(6, 0, [portal_group.id])],
        })

        cls.hub_seller = cls.env['logistics.seller'].create({
            'name': 'Print Ready Hub Seller',
            'zip': '682001',
        })
        cls.hub_seller.write({'fulfilment_method': 'own_network'})
        cls.hub_login = 'kx_print_ready_hub'
        cls.env['res.users'].create({
            'name': 'Print Ready Hub',
            'login': cls.hub_login,
            'password': cls.hub_login,
            'partner_id': cls.hub_seller.partner_id.id,
            'group_ids': [(6, 0, [portal_group.id])],
        })

    @classmethod
    def _new_shipment(cls, seller, state='pickup_requested', **overrides):
        order = cls.env['logistics.order'].create({'seller_id': seller.id})
        vals = {
            'order_id': order.id,
            'seller_id': seller.id,
            'shipping_to_name': 'Print Ready Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Print ready article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        }
        vals.update(overrides)
        shipment = cls.env['logistics.shipment'].create(vals)
        if state != 'order_added':
            shipment.sudo().with_context(
                allow_shipment_state_write=True,
            ).write({'state': state})
        return shipment

    def _csrf(self, html):
        match = re.search(r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html)
        self.assertTrue(match, 'no csrf_token in the rendered page')
        return match.group(1)

    def _print_redirect(self, path):
        return self.url_open(path, allow_redirects=False)

    def _status(self, shipments=(), orders=()):
        response = self.url_open('/my/print_status?shipments=%s&orders=%s' % (
            ','.join(str(i) for i in shipments),
            ','.join(str(i) for i in orders),
        ))
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _radio(self, html, code):
        match = re.search(
            r'<input[^>]*id="kx_ship_article_%s"[^>]*>' % code, html)
        self.assertTrue(match, 'no %s service radio' % code)
        return match.group(0)

    def _state_select(self, html):
        match = re.search(
            r'<select name="shipping_to_state_id".*?</select>', html, re.DOTALL)
        self.assertTrue(match, 'no destination state select')
        return match.group(0)

    def _state_option(self, select_html, state):
        match = re.search(r'<option[^>]*value="%d"[^>]*>' % state.id, select_html)
        self.assertTrue(match, '%s missing from the state select' % state.name)
        return match.group(0)

    # ------------------------------------------------------------------
    # Form defaults
    # ------------------------------------------------------------------
    def test_manual_order_defaults_to_business_parcel_and_kerala(self):
        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/orders/manual')
        self.assertEqual(form.status_code, 200)

        self.assertIn('checked', self._radio(form.text, 'BP'))
        self.assertNotIn('checked', self._radio(form.text, 'SP'))
        self.assertIn('Below 500 g', form.text)

        select = self._state_select(form.text)
        self.assertIn('selected', self._state_option(select, self.kerala))
        self.assertEqual(select.count('selected="selected"'), 1)
        andaman = self.env['res.country.state'].search([
            ('country_id', '=', self.env.ref('base.in').id),
            ('name', 'ilike', 'Andaman'),
        ], limit=1)
        if andaman:
            self.assertIn(
                'value="%d"' % andaman.id, select,
                'other states must still be offered')
            self.assertNotIn('selected', self._state_option(select, andaman))

    def test_own_network_manual_order_defaults_to_kerala(self):
        self.authenticate(self.hub_login, self.hub_login)
        form = self.url_open('/my/orders/manual')
        self.assertEqual(form.status_code, 200)
        self.assertNotIn('name="indiapost_article_type"', form.text)
        select = self._state_select(form.text)
        self.assertIn('selected', self._state_option(select, self.kerala))

    def test_add_shipment_and_bulk_forms_default_to_business_parcel(self):
        self.authenticate(self.ip_login, self.ip_login)
        new = self.url_open('/my/shipments/new')
        self.assertIn('checked', self._radio(new.text, 'BP'))
        self.assertIn(
            'selected', self._state_option(self._state_select(new.text), self.kerala))

        bulk = self.url_open('/my/orders/new')
        match = re.search(r'<input[^>]*id="kx_bulk_article_BP"[^>]*>', bulk.text)
        self.assertTrue(match)
        self.assertIn('checked', match.group(0))

    def test_edit_keeps_the_sellers_state_and_service(self):
        shipment = self._new_shipment(
            self.ip_seller, state='order_added',
            shipping_to_state_id=self.tamil_nadu.id,
            indiapost_article_type='SP',
        )
        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/shipments/%s/edit' % shipment.id)
        self.assertEqual(form.status_code, 200)
        select = self._state_select(form.text)
        self.assertIn('selected', self._state_option(select, self.tamil_nadu))
        self.assertNotIn('selected', self._state_option(select, self.kerala))
        self.assertIn('checked', self._radio(form.text, 'SP'))
        self.assertNotIn('checked', self._radio(form.text, 'BP'))

    def test_seller_can_still_choose_speed_post(self):
        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/orders/manual')
        self.assertNotIn('disabled', self._radio(form.text, 'SP'))
        self.url_open('/my/orders/create', data={
            'csrf_token': self._csrf(form.text),
            'shipping_to_name': 'Speed Post Customer',
            'shipping_to_mobile': '9876543210',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_state_id': str(self.kerala.id),
            'item_description': 'Toys',
            'total_weight': '1.5',
            'order_payment_type': 'prepaid',
            'pickup_date': fields.Date.context_today(self.env.user).isoformat(),
            'length_cm': '30',
            'breadth_cm': '20',
            'height_cm': '15',
            'indiapost_article_type': 'SP',
        })
        self.env.invalidate_all()
        order = self.env['logistics.order'].search(
            [('seller_id', '=', self.ip_seller.id)], order='id desc', limit=1)
        self.assertTrue(order)
        self.assertEqual(order.shipment_ids.indiapost_article_type, 'SP')

    # ------------------------------------------------------------------
    # Print waits for the ARN
    # ------------------------------------------------------------------
    def test_indiapost_without_arn_print_is_pending_not_a_pdf_link(self):
        shipment = self._new_shipment(self.ip_seller)
        order = shipment.order_id
        self.assertEqual(order.state, 'pickup_requested')
        self.assertFalse(shipment.indiapost_article_number)
        self.assertEqual(shipment.portal_awb_print_state(), 'pending')
        self.assertEqual(order.portal_awb_print_state(), 'pending')
        self.assertFalse(shipment.portal_awb_printable())
        self.assertFalse(order.portal_awb_printable())

        self.authenticate(self.ip_login, self.ip_login)
        ship_href = '/my/shipments/%s/print' % shipment.id
        order_href = '/my/orders/%s/print' % order.id

        listing = self.url_open('/my/shipments')
        self.assertNotIn(ship_href, listing.text)
        self.assertIn('kx-print-awb-pending', listing.text)
        self.assertIn('data-kx-print-id="%s"' % shipment.id, listing.text)
        self.assertIn('data-kx-print-state="pending"', listing.text)
        self.assertIn('class="kx-print-pending-loading"', listing.text)

        detail = self.url_open('/my/shipments/%s' % shipment.id)
        self.assertNotIn(ship_href, detail.text)
        self.assertIn('Preparing label...', detail.text)
        buttons = [
            tag for tag in re.findall(r'<button[^>]*>', detail.text)
            if 'kx-print-awb-pending' in tag
        ]
        self.assertEqual(len(buttons), 1)
        self.assertIn('disabled', buttons[0])
        self.assertNotIn('href=', buttons[0])

        order_detail = self.url_open('/my/orders/%s' % order.id)
        self.assertNotIn(order_href, order_detail.text)
        self.assertIn('data-kx-print-kind="order"', order_detail.text)

        orders = self.url_open('/my/orders')
        self.assertNotIn(order_href, orders.text)

        for path, expected in ((ship_href, '/my/shipments'),
                               (order_href, '/my/orders/%s' % order.id)):
            denied = self._print_redirect(path + '?paper=100x150')
            self.assertIn(denied.status_code, (301, 302, 303, 307))
            location = denied.headers.get('Location', '')
            self.assertIn(expected, location)
            self.assertNotIn('/report/pdf', location)
        follow = self.url_open(ship_href)
        self.assertIn('India Post booking is still in progress', follow.text)

        with self.assertRaises(AccessError):
            self.env['ir.actions.report'].with_user(self.ip_user)._render_qweb_html(
                'keralariders_logistics.report_shipment_document_100x150',
                shipment.ids,
            )

        status = self._status(shipments=[shipment.id], orders=[order.id])
        self.assertEqual(status['shipments'], {str(shipment.id): 'pending'})
        self.assertEqual(status['orders'], {str(order.id): 'pending'})

    def test_indiapost_with_arn_prints(self):
        shipment = self._new_shipment(self.ip_seller)
        order = shipment.order_id
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
        })
        self.assertEqual(shipment.portal_awb_print_state(), 'ready')
        self.assertTrue(shipment.portal_awb_printable())
        self.assertTrue(order.portal_awb_printable())

        self.authenticate(self.ip_login, self.ip_login)
        status = self._status(shipments=[shipment.id], orders=[order.id])
        self.assertEqual(status['shipments'], {str(shipment.id): 'ready'})
        self.assertEqual(status['orders'], {str(order.id): 'ready'})

        detail = self.url_open('/my/shipments/%s' % shipment.id)
        self.assertIn('href="/my/shipments/%s/print"' % shipment.id, detail.text)
        self.assertNotIn('kx-print-awb-pending', detail.text)
        order_detail = self.url_open('/my/orders/%s' % order.id)
        self.assertIn('href="/my/orders/%s/print"' % order.id, order_detail.text)
        self.assertIn('Print AWBs', order_detail.text)

        allowed = self._print_redirect(
            '/my/shipments/%s/print?paper=100x150' % shipment.id)
        self.assertIn(
            '/report/pdf/keralariders_logistics.action_report_shipment_100x150/%s'
            % shipment.id,
            allowed.headers.get('Location', ''),
        )

        thermal = self.env['ir.actions.report'].with_user(self.ip_user)._render_qweb_html(
            'keralariders_logistics.report_shipment_document_100x150',
            shipment.ids,
        )[0]
        if isinstance(thermal, bytes):
            thermal = thermal.decode('utf-8')
        self.assertIn(ARTICLE, thermal)
        self.assertNotIn('kx-label-awb-barcode', thermal)

    def test_failed_booking_stops_polling_and_shows_in_progress(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({'indiapost_booking_state': 'error'})
        self.assertEqual(shipment.portal_awb_print_state(), 'failed')
        self.assertFalse(shipment.portal_awb_printable())

        self.authenticate(self.ip_login, self.ip_login)
        detail = self.url_open('/my/shipments/%s' % shipment.id)
        self.assertNotIn('/my/shipments/%s/print' % shipment.id, detail.text)
        self.assertIn('data-kx-print-state="failed"', detail.text)
        self.assertIn('India Post booking is still in progress', detail.text)
        self.assertEqual(
            self._status(shipments=[shipment.id])['shipments'],
            {str(shipment.id): 'failed'},
        )

    def test_own_network_prints_immediately(self):
        shipment = self._new_shipment(self.hub_seller)
        self.assertEqual(shipment.portal_awb_print_state(), 'ready')
        self.authenticate(self.hub_login, self.hub_login)
        listing = self.url_open('/my/shipments')
        self.assertIn('href="/my/shipments/%s/print"' % shipment.id, listing.text)
        self.assertNotIn('kx-print-awb-pending', listing.text)

    def test_draft_and_cancelled_stay_non_printable(self):
        draft = self._new_shipment(self.ip_seller, state='order_added')
        cancelled = self._new_shipment(self.ip_seller, state='cancelled')
        for shipment in (draft, cancelled):
            self.assertFalse(shipment.portal_awb_print_state())
            self.assertFalse(shipment.portal_awb_printable())

        self.authenticate(self.ip_login, self.ip_login)
        listing = self.url_open('/my/shipments')
        for shipment in (draft, cancelled):
            self.assertNotIn('/my/shipments/%s/print' % shipment.id, listing.text)
            self.assertNotIn('data-kx-print-id="%s"' % shipment.id, listing.text)

    def test_status_route_only_reports_own_records(self):
        shipment = self._new_shipment(self.ip_seller)
        self.authenticate(self.hub_login, self.hub_login)
        status = self._status(shipments=[shipment.id], orders=[shipment.order_id.id])
        self.assertEqual(status, {'shipments': {}, 'orders': {}})
