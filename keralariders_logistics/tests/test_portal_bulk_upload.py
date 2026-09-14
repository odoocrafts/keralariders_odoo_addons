"""Bulk CSV upload: India Post sellers pick Speed Post vs Business Parcel.

The radios live on the order form (like pickup date), not as a CSV column.
Preview quotes through the same ``_ip_quote_and_store`` helper as Add Order;
hub sellers never see the field and never hit the India Post tariff client.
"""

import io
import re
from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.models.indiapost_tariff import (
    BUSINESS_PARCEL_TARIFF_PATH,
    SPEED_POST_TARIFF_PATH,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin
from odoo.addons.keralariders_logistics.tests.test_indiapost_tariff import (
    BP_PAYLOAD,
    SP_PAYLOAD,
)


def _response(payload):
    return type('FakeTariffResponse', (), {'payload': payload})()


CSV_HEADERS = (
    'Customer Name*,Phone Number* (mandatory),Address* (mandatory),'
    'Pincode* (mandatory),Weight (kg)*,Item Description*,'
    'Payment Type (prepaid/cod),Total Order Value,'
    'Length (cm)*,Breadth (cm)*,Height (cm)*'
)
CSV_ROWS = (
    'John Doe,9876543210,"123 Main St, Apt 4B",695001,1.5,Electronics,prepaid,0,30,20,15\n'
    'Jane Smith,9988776655,456 Market Road,695001,2.0,Clothing,prepaid,0,25,18,10\n'
)


@tagged('post_install', '-at_install')
class TestPortalBulkUpload(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)

        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Bulk Upload IP Seller',
            'zip': '682001',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.ip_login = 'kx_bulk_upload_ip'
        cls.env['res.users'].create({
            'name': 'Bulk Upload IP Portal',
            'login': cls.ip_login,
            'password': cls.ip_login,
            'partner_id': cls.ip_seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

        cls.hub_seller = cls.env['logistics.seller'].create({
            'name': 'Bulk Upload Hub Seller',
            'zip': '682001',
        })
        cls.hub_seller.write({'fulfilment_method': 'own_network'})
        cls.hub_login = 'kx_bulk_upload_hub'
        cls.env['res.users'].create({
            'name': 'Bulk Upload Hub Portal',
            'login': cls.hub_login,
            'password': cls.hub_login,
            'partner_id': cls.hub_seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def _csrf(self, html):
        match = re.search(r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html)
        self.assertTrue(match, 'no csrf_token in the rendered page')
        return match.group(1)

    def _hidden_value(self, html, name):
        match = re.search(
            r'name="%s"[^>]*\bvalue="([^"]*)"' % re.escape(name), html)
        self.assertTrue(match, 'no %s in the rendered page' % name)
        return match.group(1)

    def _csv_file(self):
        return ('bulk.csv', io.BytesIO(
            (CSV_HEADERS + '\n' + CSV_ROWS).encode('utf-8')
        ), 'text/csv')

    def _orders_of(self, seller):
        return self.env['logistics.order'].search([('seller_id', '=', seller.id)])

    def _wallet_of(self, seller):
        return self.env['logistics.wallet'].search(
            [('seller_id', '=', seller.id)], limit=1)

    def _credit_wallet(self, seller, amount=5000.0):
        wallet = self._wallet_of(seller)
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': wallet.id,
            'amount': amount,
            'reference': 'Test top-up',
        })
        wallet.invalidate_recordset(['balance'])
        return wallet

    def _upload(self, form_html, **post):
        data = {
            'csrf_token': self._csrf(form_html),
            'pickup_date': fields.Date.context_today(self.env.user).isoformat(),
            'bulk_upload_token': self._hidden_value(
                form_html, 'bulk_upload_token'),
        }
        data.update(post)
        return self.url_open(
            '/my/orders/bulk_upload',
            data=data,
            files={'csv_file': self._csv_file()},
        )

    def _confirm_pickup(self, order, detail_html=None):
        if detail_html is None:
            detail = self.url_open('/my/orders/%s' % order.id)
            self.assertEqual(detail.status_code, 200)
            detail_html = detail.text
        return self.url_open('/my/orders/request_pickup', data={
            'csrf_token': self._csrf(detail_html),
            'order_id': str(order.id),
            'pickup_confirm_token': self._hidden_value(
                detail_html, 'pickup_confirm_token'),
        })

    def _mock_tariff_client(self, captured):
        def fake_call(this, method, path, params=None, **kwargs):
            captured.append({'method': method, 'path': path, 'params': params})
            if path == BUSINESS_PARCEL_TARIFF_PATH:
                return _response(BP_PAYLOAD)
            if path == SPEED_POST_TARIFF_PATH:
                return _response(SP_PAYLOAD)
            raise AssertionError('unexpected India Post path %s' % path)

        return patch.object(
            self.registry['logistics.indiapost.client'], 'call', fake_call,
        )

    def test_indiapost_bulk_form_offers_speed_post_and_business_parcel(self):
        self.authenticate(self.ip_login, self.ip_login)
        page = self.url_open('/my/orders/new')
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="indiapost_article_type"', page.text)
        self.assertIn('Speed Post', page.text)
        self.assertIn('Business Parcel', page.text)
        self.assertIn('normal parcel', page.text)
        self.assertIn('kx_bulk_article_SP', page.text)
        self.assertIn('This choice applies to every row', page.text)
        self.assertIn('name="bulk_upload_token"', page.text)

    def test_own_network_bulk_form_hides_india_post_service(self):
        self.authenticate(self.hub_login, self.hub_login)
        page = self.url_open('/my/orders/new')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('name="indiapost_article_type"', page.text)
        self.assertNotIn('Business Parcel', page.text)

    def test_indiapost_bulk_with_business_parcel_quotes_bp_path(self):
        captured = []
        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/orders/new')
        with self._mock_tariff_client(captured):
            result = self._upload(form.text, indiapost_article_type='BP')
        self.assertEqual(result.status_code, 200)

        self.env.invalidate_all()
        order = self._orders_of(self.ip_seller)
        self.assertEqual(len(order), 1)
        self.assertEqual(len(order.shipment_ids), 2)
        self.assertEqual(set(order.shipment_ids.mapped('indiapost_article_type')),
                         {'BP'})
        self.assertEqual(set(order.shipment_ids.mapped('fulfilment_method')),
                         {'indiapost'})
        self.assertTrue(captured)
        self.assertTrue(
            all(entry['path'] == BUSINESS_PARCEL_TARIFF_PATH for entry in captured),
            captured,
        )
        self.assertTrue(
            all(entry['params']['product-code'] == ipc.ARTICLE_TYPE_BUSINESS_PARCEL
                for entry in captured),
            captured,
        )

    def test_indiapost_bulk_defaults_to_speed_post(self):
        captured = []
        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/orders/new')
        with self._mock_tariff_client(captured):
            self._upload(form.text)
        self.env.invalidate_all()
        order = self._orders_of(self.ip_seller)
        self.assertEqual(len(order), 1)
        self.assertEqual(set(order.shipment_ids.mapped('indiapost_article_type')),
                         {'SP'})
        self.assertTrue(captured)
        self.assertTrue(
            all(entry['path'] == SPEED_POST_TARIFF_PATH for entry in captured),
            captured,
        )

    def test_own_network_bulk_does_not_quote_or_store_posted_article_type(self):
        captured = []
        self.authenticate(self.hub_login, self.hub_login)
        form = self.url_open('/my/orders/new')
        with self._mock_tariff_client(captured):
            self._upload(form.text, indiapost_article_type='BP')
        self.env.invalidate_all()
        order = self._orders_of(self.hub_seller)
        self.assertEqual(len(order), 1)
        shipments = order.shipment_ids
        self.assertEqual(len(shipments), 2)
        self.assertEqual(set(shipments.mapped('fulfilment_method')),
                         {'own_network'})
        self.assertEqual(
            set(shipments.mapped('indiapost_article_type')), {'SP'},
            'hub bulk must ignore the posted India Post product',
        )
        self.assertFalse(captured, 'hub bulk must not call the India Post client')

    def test_cannot_inject_fulfilment_method_on_bulk_upload(self):
        self.authenticate(self.hub_login, self.hub_login)
        form = self.url_open('/my/orders/new')
        self._upload(
            form.text,
            fulfilment_method='indiapost',
            delivery_charges_total='1.0',
        )
        self.env.invalidate_all()
        order = self._orders_of(self.hub_seller)
        self.assertEqual(len(order), 1)
        shipments = order.shipment_ids
        self.assertEqual(set(shipments.mapped('fulfilment_method')),
                         {'own_network'})
        for shipment in shipments:
            self.assertNotAlmostEqual(
                shipment.delivery_charges_total, 1.0, places=2,
            )

    def test_preview_then_confirm_creates_one_order_and_debits_once(self):
        """Preview persists the draft; Confirm only requests pickup on it."""
        self.authenticate(self.hub_login, self.hub_login)
        wallet = self._credit_wallet(self.hub_seller)
        opening = wallet.balance

        form = self.url_open('/my/orders/new')
        preview = self._upload(form.text)
        self.assertEqual(preview.status_code, 200)
        self.env.invalidate_all()
        order = self._orders_of(self.hub_seller)
        self.assertEqual(len(order), 1)
        self.assertEqual(order.state, 'draft')
        self.assertEqual(len(order.shipment_ids), 2)
        charge = order.total_charges
        self.assertGreater(charge, 0.0)
        wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(wallet.balance, opening, places=2)

        confirm = self._confirm_pickup(order)
        self.assertEqual(confirm.status_code, 200)
        self.env.invalidate_all()
        order = self._orders_of(self.hub_seller)
        self.assertEqual(len(order), 1)
        self.assertEqual(order.state, 'pickup_requested')
        wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(wallet.balance, opening - charge, places=2)
        self.assertEqual(
            len(self.env['logistics.wallet.transaction'].search([
                ('wallet_id', '=', wallet.id),
                ('shipment_id', 'in', order.shipment_ids.ids),
            ])),
            2,
        )

    def test_double_preview_submit_creates_one_order(self):
        """The one-shot token is the double-click guard on Preview."""
        self.authenticate(self.hub_login, self.hub_login)
        form = self.url_open('/my/orders/new')
        token = self._hidden_value(form.text, 'bulk_upload_token')
        first = self._upload(form.text)
        self.assertEqual(first.status_code, 200)
        second = self._upload(form.text, bulk_upload_token=token)
        self.assertEqual(second.status_code, 200)
        self.env.invalidate_all()
        orders = self._orders_of(self.hub_seller)
        self.assertEqual(len(orders), 1)
        self.assertIn('already submitted', second.text)

    def test_double_confirm_does_not_debit_twice(self):
        self.authenticate(self.hub_login, self.hub_login)
        wallet = self._credit_wallet(self.hub_seller)
        opening = wallet.balance

        form = self.url_open('/my/orders/new')
        self._upload(form.text)
        self.env.invalidate_all()
        order = self._orders_of(self.hub_seller)
        self.assertEqual(len(order), 1)
        charge = order.total_charges

        detail = self.url_open('/my/orders/%s' % order.id)
        token = self._hidden_value(detail.text, 'pickup_confirm_token')
        first = self._confirm_pickup(order, detail.text)
        self.assertEqual(first.status_code, 200)
        second = self.url_open('/my/orders/request_pickup', data={
            'csrf_token': self._csrf(detail.text),
            'order_id': str(order.id),
            'pickup_confirm_token': token,
        })
        self.assertEqual(second.status_code, 200)
        self.env.invalidate_all()
        orders = self._orders_of(self.hub_seller)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders.state, 'pickup_requested')
        wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(wallet.balance, opening - charge, places=2)
        self.assertEqual(
            len(self.env['logistics.wallet.transaction'].search([
                ('wallet_id', '=', wallet.id),
                ('amount', '<', 0),
            ])),
            2,
        )
