"""Auto-book India Post on pickup. Print AWB stays a single page.

The HTTP client is mocked: these tests never consume a live AWB and never
leave the process. Barcodes, when allocated, come from the TT test range.
The official CEPT sticker is stored for Print India Post Label, not merged.
"""

import base64
import io
import re

from odoo import fields
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.tests.common import (
    IP_NETWORK_BLOCKED,
    IndiapostHermeticMixin,
)


@tagged('post_install', '-at_install')
class TestIndiapostAutobook(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Autobook IP Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Kochi Head Office',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.own_seller = cls.env['logistics.seller'].create({
            'name': 'Autobook Hub Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Kochi Head Office',
        })
        cls.own_seller.write({'fulfilment_method': 'own_network'})
        cls.ip_wallet = cls.ip_seller.wallet_ids[0]
        cls.own_wallet = cls.own_seller.wallet_ids[0]

    def setUp(self):
        super().setUp()
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.ip_wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        self.ip_wallet.invalidate_recordset(['balance'])
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.own_wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        self.own_wallet.invalidate_recordset(['balance'])

    def _new_order(self, seller, **overrides):
        order = self.env['logistics.order'].create({'seller_id': seller.id})
        vals = {
            'order_id': order.id,
            'seller_id': seller.id,
            'shipping_to_name': 'Autobook Customer',
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
        shipment = self.env['logistics.shipment'].create(vals)
        return order, shipment

    def _store_quote(self, shipment, total=118.0, base=100.0, tax=18.0):
        shipment.write({
            'indiapost_base_tariff': base,
            'indiapost_vas_charges': 0.0,
            'indiapost_tax_amount': tax,
            'indiapost_total_tariff': total,
            'indiapost_quoted_weight_g': ipc.band_weight(
                ipc.kg_to_grams(shipment.total_weight)),
            'indiapost_tariff_quoted_on': fields.Datetime.now(),
            'indiapost_quote_signature': shipment._ip_quote_signature(),
        })

    def _slab_price(self, shipment):
        return self.env['logistics.delivery.charges'].calculate_delivery_charge(
            shipment.total_weight,
            shipment.shipping_from_district_id == shipment.shipping_to_district_id,
            package_id=None,
        )

    def test_pickup_on_indiapost_order_books_and_fetches_label(self):
        order, shipment = self._new_order(self.ip_seller)
        self._store_quote(shipment, total=118.0)
        opening = self.ip_wallet.balance

        with self._ip_patch_call() as mocked:
            order.action_request_pickup()

        kinds = []
        for call in mocked.call_args_list:
            operation = call.kwargs.get('operation')
            path = call.kwargs.get('path')
            if path is None and len(call.args) >= 3:
                path = call.args[2]
            if operation:
                kinds.append(operation)
            elif path and 'label' in str(path):
                kinds.append('label')
            elif path and 'process-articles' in str(path):
                kinds.append('booking')
        self.assertIn('booking', kinds)
        self.assertIn('label', kinds)

        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertEqual(shipment.indiapost_booking_state, 'booked')
        self.assertTrue(shipment.indiapost_article_number)
        self.assertTrue(shipment.indiapost_label_pdf)
        self.assertTrue(shipment.wallet_transaction_id)
        self.assertAlmostEqual(shipment.wallet_transaction_id.amount, -118.0,
                               places=2)
        self.ip_wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.ip_wallet.balance, opening - 118.0, places=2)
        slab = self._slab_price(shipment)
        self.assertNotAlmostEqual(slab, 118.0, places=2)

    def test_own_network_pickup_does_not_book(self):
        order, shipment = self._new_order(self.own_seller)
        self.assertEqual(shipment.fulfilment_method, 'own_network')

        with self._ip_patch_call() as mocked:
            order.action_request_pickup()

        mocked.assert_not_called()
        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertEqual(shipment.indiapost_booking_state, 'not_required')
        self.assertFalse(shipment.indiapost_article_number)
        self.assertTrue(shipment.wallet_transaction_id)

    def test_already_booked_is_not_booked_twice(self):
        order, shipment = self._new_order(self.ip_seller)
        self._store_quote(shipment)
        shipment.sudo().write({
            'indiapost_article_number': 'TT900009999IN',
            'indiapost_booking_state': 'booked',
            'indiapost_label_pdf': base64.b64encode(self._ip_blank_pdf_bytes()),
            'indiapost_label_filename': 'TT900009999IN.pdf',
        })

        with self._ip_patch_call() as mocked:
            order.action_request_pickup()

        mocked.assert_not_called()
        self.assertEqual(shipment.indiapost_booking_state, 'booked')
        self.assertEqual(shipment.indiapost_article_number, 'TT900009999IN')
        self.assertEqual(shipment.state, 'pickup_requested')

    def test_booking_failure_keeps_pickup_and_debit(self):
        """Fail closed: pickup stays requested, error is on the shipment."""
        order, shipment = self._new_order(self.ip_seller)
        self._store_quote(shipment, total=118.0)

        order.action_request_pickup()

        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertTrue(shipment.wallet_transaction_id)
        self.assertEqual(shipment.indiapost_booking_state, 'error')
        self.assertTrue(shipment.indiapost_booking_error)
        self.assertIn(IP_NETWORK_BLOCKED, shipment.indiapost_booking_error)

    def test_print_awb_stays_one_page_when_label_present(self):
        _order, shipment = self._new_order(self.ip_seller)
        kx = self._ip_blank_pdf_bytes()
        label = self._ip_blank_pdf_bytes()
        shipment.sudo().write({
            'indiapost_article_number': 'EY547878418IN',
            'indiapost_booking_state': 'booked',
            'indiapost_label_pdf': base64.b64encode(label),
            'indiapost_label_filename': 'EY547878418IN.pdf',
        })
        collected = {shipment.id: {'stream': io.BytesIO(kx)}}
        result = self.env['ir.actions.report']._ip_append_indiapost_labels(
            collected)
        out = result[shipment.id]['stream'].getvalue()
        self.assertEqual(self._ip_pdf_page_count(out), 1)
        self.assertEqual(out, kx)

    def test_print_awb_stays_one_page_without_label(self):
        _order, shipment = self._new_order(self.ip_seller)
        kx = self._ip_blank_pdf_bytes()
        shipment.sudo().write({
            'indiapost_article_number': 'EY547878418IN',
            'indiapost_booking_state': 'booked',
        })
        collected = {shipment.id: {'stream': io.BytesIO(kx)}}
        result = self.env['ir.actions.report']._ip_append_indiapost_labels(
            collected)
        out = result[shipment.id]['stream'].getvalue()
        self.assertEqual(self._ip_pdf_page_count(out), 1)
        self.assertEqual(out, kx)

    def test_own_network_print_awb_is_unchanged(self):
        _order, shipment = self._new_order(self.own_seller)
        kx = self._ip_blank_pdf_bytes()
        collected = {shipment.id: {'stream': io.BytesIO(kx)}}
        result = self.env['ir.actions.report']._ip_append_indiapost_labels(
            collected)
        self.assertEqual(result[shipment.id]['stream'].getvalue(), kx)

    def test_report_hook_does_not_append_cept_label(self):
        _order, shipment = self._new_order(self.ip_seller)
        kx = self._ip_blank_pdf_bytes()
        shipment.sudo().write({
            'indiapost_article_number': 'EY547878418IN',
            'indiapost_booking_state': 'booked',
            'indiapost_label_pdf': base64.b64encode(self._ip_blank_pdf_bytes()),
            'indiapost_label_filename': 'EY547878418IN.pdf',
        })
        collected = {shipment.id: {'stream': io.BytesIO(kx)}}
        result = self.env['ir.actions.report']._ip_append_indiapost_labels(
            collected)
        out = result[shipment.id]['stream'].getvalue()
        self.assertEqual(self._ip_pdf_page_count(out), 1)
        self.assertEqual(out, kx)


@tagged('post_install', '-at_install')
class TestIndiapostAutobookPortal(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Autobook Portal IP Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Kochi Head Office',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.ip_login = 'kx_autobook_portal_ip'
        cls.env['res.users'].create({
            'name': 'Autobook Portal IP',
            'login': cls.ip_login,
            'password': cls.ip_login,
            'partner_id': cls.ip_seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        cls.own_seller = cls.env['logistics.seller'].create({
            'name': 'Autobook Portal Hub Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Kochi Head Office',
        })
        cls.own_seller.write({'fulfilment_method': 'own_network'})
        cls.own_login = 'kx_autobook_portal_hub'
        cls.env['res.users'].create({
            'name': 'Autobook Portal Hub',
            'login': cls.own_login,
            'password': cls.own_login,
            'partner_id': cls.own_seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def setUp(self):
        super().setUp()
        for wallet in (self.ip_seller.wallet_ids[0],
                       self.own_seller.wallet_ids[0]):
            self.env['logistics.wallet.transaction'].create({
                'wallet_id': wallet.id,
                'amount': 5000.0,
                'reference': 'Test top-up',
            })
            wallet.invalidate_recordset(['balance'])

    def _csrf(self, html):
        match = re.search(r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html)
        self.assertTrue(match, 'no csrf_token in the rendered page')
        return match.group(1)

    def _store_quote(self, shipment, total=118.0):
        shipment.write({
            'indiapost_base_tariff': 100.0,
            'indiapost_vas_charges': 0.0,
            'indiapost_tax_amount': 18.0,
            'indiapost_total_tariff': total,
            'indiapost_quoted_weight_g': ipc.band_weight(
                ipc.kg_to_grams(shipment.total_weight)),
            'indiapost_tariff_quoted_on': fields.Datetime.now(),
            'indiapost_quote_signature': shipment._ip_quote_signature(),
        })

    def _create_portal_order(self, login, **overrides):
        self.authenticate(login, login)
        form = self.url_open('/my/orders/manual')
        self.assertEqual(form.status_code, 200)
        data = {
            'csrf_token': self._csrf(form.text),
            'shipping_to_name': 'Portal Autobook Customer',
            'shipping_to_mobile': '9876543210',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'item_description': 'Toys',
            'total_weight': '1.5',
            'length_cm': '30',
            'breadth_cm': '20',
            'height_cm': '15',
            'order_payment_type': 'prepaid',
            'pickup_date': fields.Date.context_today(self.env.user).isoformat(),
        }
        data.update(overrides)
        result = self.url_open('/my/orders/create', data=data)
        self.assertEqual(result.status_code, 200)
        self.env.invalidate_all()
        return result

    def test_portal_confirm_on_indiapost_seller_books_and_fetches_label(self):
        self._create_portal_order(self.ip_login)
        order = self.env['logistics.order'].search(
            [('seller_id', '=', self.ip_seller.id)], limit=1)
        self.assertTrue(order)
        shipment = order.shipment_ids
        self.assertEqual(shipment.fulfilment_method, 'indiapost')
        self._store_quote(shipment)

        detail = self.url_open('/my/orders/%s' % order.id)
        self.assertEqual(detail.status_code, 200)
        with self._ip_patch_call():
            pickup = self.url_open('/my/orders/request_pickup', data={
                'csrf_token': self._csrf(detail.text),
                'order_id': str(order.id),
            })
        self.assertEqual(pickup.status_code, 200)
        self.env.invalidate_all()
        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertEqual(shipment.indiapost_booking_state, 'booked')
        self.assertTrue(shipment.indiapost_article_number)
        self.assertTrue(shipment.indiapost_label_pdf)
        self.assertIn('Pickup requested', pickup.text)
        self.assertNotIn('could not book the shipment', pickup.text)

    def test_portal_confirm_on_own_network_does_not_book(self):
        self._create_portal_order(self.own_login)
        order = self.env['logistics.order'].search(
            [('seller_id', '=', self.own_seller.id)], limit=1)
        shipment = order.shipment_ids
        self.assertEqual(shipment.fulfilment_method, 'own_network')

        detail = self.url_open('/my/orders/%s' % order.id)
        with self._ip_patch_call() as mocked:
            pickup = self.url_open('/my/orders/request_pickup', data={
                'csrf_token': self._csrf(detail.text),
                'order_id': str(order.id),
            })
        self.assertEqual(pickup.status_code, 200)
        mocked.assert_not_called()
        self.env.invalidate_all()
        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertFalse(shipment.indiapost_article_number)

    def test_portal_confirm_shows_india_post_error_when_booking_fails(self):
        self._create_portal_order(self.ip_login)
        order = self.env['logistics.order'].search(
            [('seller_id', '=', self.ip_seller.id)], limit=1)
        shipment = order.shipment_ids
        self._store_quote(shipment)

        detail = self.url_open('/my/orders/%s' % order.id)
        pickup = self.url_open('/my/orders/request_pickup', data={
            'csrf_token': self._csrf(detail.text),
            'order_id': str(order.id),
        })
        self.assertEqual(pickup.status_code, 200)
        self.env.invalidate_all()
        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertEqual(shipment.indiapost_booking_state, 'error')
        self.assertIn('could not book the shipment', pickup.text)
        self.assertIn(IP_NETWORK_BLOCKED, pickup.text)
        self.assertTrue(shipment.wallet_transaction_id)
