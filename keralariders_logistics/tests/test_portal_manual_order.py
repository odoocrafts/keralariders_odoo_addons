"""Sellers can raise one order and its shipments from the portal, without Excel.

The new routes must reuse the same create / charge / fulfilment path as bulk
upload: a crafted POST cannot pick a carrier or a price, and a validation
error cannot leave a headless order behind.
"""

import re

from odoo import fields
from odoo.tests import HttpCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


@tagged('post_install', '-at_install')
class TestPortalManualOrder(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)

        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Manual Order Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'own_network'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.portal_login = 'kx_manual_order'
        cls.portal_user = cls.env['res.users'].create({
            'name': 'Manual Order Portal',
            'login': cls.portal_login,
            'password': cls.portal_login,
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Manual Order IP Seller',
            'zip': '682001',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.ip_login = 'kx_manual_order_ip'
        cls.ip_user = cls.env['res.users'].create({
            'name': 'Manual Order IP Portal',
            'login': cls.ip_login,
            'password': cls.ip_login,
            'partner_id': cls.ip_seller.partner_id.id,
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

    def _shipment_post(self, csrf, **overrides):
        data = {
            'csrf_token': csrf,
            'shipping_to_name': 'Web Order Customer',
            'shipping_to_mobile': '9876543210',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'item_description': 'Toys',
            'total_weight': '1.5',
            'order_payment_type': 'prepaid',
            'pickup_date': fields.Date.context_today(self.env.user).isoformat(),
        }
        data.update(overrides)
        return data

    def _orders_of(self, seller):
        return self.env['logistics.order'].search([('seller_id', '=', seller.id)])

    def _rate_card(self, shipment):
        return self.env['logistics.delivery.charges'].calculate_delivery_charge(
            shipment.total_weight,
            shipment.shipping_from_district_id == shipment.shipping_to_district_id,
            package_id=self.seller.delivery_package_id.id or None,
        )

    def test_orders_list_offers_add_order_next_to_bulk_upload(self):
        self.authenticate(self.portal_login, self.portal_login)
        page = self.url_open('/my/orders')
        self.assertEqual(page.status_code, 200)
        self.assertIn('/my/orders/manual', page.text)
        self.assertIn('Add Order', page.text)
        self.assertIn('/my/orders/new', page.text)
        self.assertIn('Bulk Upload Order', page.text)

    def test_own_network_seller_creates_an_order_and_shipment_without_excel(self):
        self.authenticate(self.portal_login, self.portal_login)
        form = self.url_open('/my/orders/manual')
        self.assertEqual(form.status_code, 200)
        self.assertIn('Create Order', form.text)
        self.assertIn('name="shipping_to_name"', form.text)

        result = self.url_open('/my/orders/create', data=self._shipment_post(
            self._csrf(form.text),
        ))
        self.assertEqual(result.status_code, 200)

        self.env.invalidate_all()
        order = self._orders_of(self.seller)
        self.assertEqual(len(order), 1, 'the portal POST did not create an order')
        self.assertEqual(len(order.shipment_ids), 1)
        shipment = order.shipment_ids
        self.assertEqual(shipment.shipping_to_name, 'Web Order Customer')
        self.assertEqual(shipment.shipping_to_zip, '695001')
        self.assertEqual(shipment.total_weight, 1.5)
        self.assertEqual(shipment.state, 'order_added')
        self.assertEqual(shipment.fulfilment_method, 'own_network')
        self.assertAlmostEqual(
            shipment.delivery_charges_total, self._rate_card(shipment), places=2,
        )
        self.assertIn('/my/orders/%s' % order.id, result.url)

        detail = self.url_open('/my/orders/%s' % order.id)
        self.assertIn('Add Shipment', detail.text)
        self.assertIn('/my/orders/%s/shipments/new' % order.id, detail.text)

    def test_a_second_shipment_can_be_added_to_the_same_draft_order(self):
        self.authenticate(self.portal_login, self.portal_login)
        form = self.url_open('/my/orders/manual')
        created = self.url_open('/my/orders/create', data=self._shipment_post(
            self._csrf(form.text),
        ))
        self.env.invalidate_all()
        order = self._orders_of(self.seller)
        self.assertEqual(len(order.shipment_ids), 1)

        add_form = self.url_open('/my/orders/%s/shipments/new' % order.id)
        self.assertEqual(add_form.status_code, 200)
        self.assertIn('Add Shipment', add_form.text)

        self.url_open(
            '/my/orders/%s/shipments/create' % order.id,
            data=self._shipment_post(
                self._csrf(add_form.text),
                shipping_to_name='Second Customer',
                shipping_to_zip='695014',
            ),
        )
        self.env.invalidate_all()
        self.assertEqual(len(order.shipment_ids), 2)
        self.assertEqual(
            set(order.shipment_ids.mapped('shipping_to_name')),
            {'Web Order Customer', 'Second Customer'},
        )
        self.assertTrue(created.url.endswith('/my/orders/%s' % order.id)
                        or ('/my/orders/%s' % order.id) in created.url)

    def test_seller_cannot_inject_fulfilment_or_delivery_charge_via_post(self):
        self.authenticate(self.portal_login, self.portal_login)
        form = self.url_open('/my/orders/manual')
        self.url_open('/my/orders/create', data=self._shipment_post(
            self._csrf(form.text),
            fulfilment_method='indiapost',
            delivery_charges_subtotal='1.0',
            delivery_charges_total='1.0',
            tax_percentage='-1',
        ))
        self.env.invalidate_all()
        order = self._orders_of(self.seller)
        self.assertEqual(len(order), 1, 'the injected POST must still create the order')
        shipment = order.shipment_ids
        self.assertEqual(
            shipment.fulfilment_method, 'own_network',
            'the seller posted a carrier and it stuck',
        )
        expected = self._rate_card(shipment)
        self.assertNotAlmostEqual(shipment.delivery_charges_total, 1.0, places=2)
        self.assertAlmostEqual(shipment.delivery_charges_total, expected, places=2)
        self.assertEqual(shipment.tax_percentage, 0.0)

    def test_validation_errors_do_not_create_a_half_order(self):
        self.authenticate(self.portal_login, self.portal_login)
        form = self.url_open('/my/orders/manual')
        csrf = self._csrf(form.text)
        before = self.env['logistics.order'].search_count([])

        missing_customer = self.url_open('/my/orders/create', data=self._shipment_post(
            csrf, shipping_to_name='',
        ))
        self.assertEqual(missing_customer.status_code, 200)

        missing_pincode = self.url_open('/my/orders/create', data=self._shipment_post(
            csrf, shipping_to_zip='',
        ))
        self.assertEqual(missing_pincode.status_code, 200)

        missing_weight = self.url_open('/my/orders/create', data=self._shipment_post(
            csrf, total_weight='0',
        ))
        self.assertEqual(missing_weight.status_code, 200)

        self.env.invalidate_all()
        self.assertEqual(
            self.env['logistics.order'].search_count([]), before,
            'a rejected form left a draft order with no shipments',
        )
        self.assertFalse(self._orders_of(self.seller))

    def test_indiapost_seller_gets_seller_fulfilment_not_posted_carrier(self):
        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/orders/manual')
        self.assertEqual(form.status_code, 200)
        self.assertIn('name="length_cm"', form.text)

        self.url_open('/my/orders/create', data=self._shipment_post(
            self._csrf(form.text),
            length_cm='30',
            breadth_cm='20',
            height_cm='15',
            fulfilment_method='own_network',
            delivery_charges_subtotal='1.0',
            delivery_charges_total='1.0',
        ))
        self.env.invalidate_all()
        order = self._orders_of(self.ip_seller)
        self.assertEqual(len(order), 1)
        shipment = order.shipment_ids
        self.assertEqual(shipment.fulfilment_method, 'indiapost')
        self.assertEqual(shipment.length_cm, 30.0)
        self.assertNotAlmostEqual(shipment.delivery_charges_total, 1.0, places=2)

    def test_cannot_add_a_shipment_to_another_sellers_order(self):
        other = self.env['logistics.order'].create({
            'seller_id': self.ip_seller.id,
        })
        self.authenticate(self.portal_login, self.portal_login)
        form = self.url_open('/my/orders/manual')
        before = len(other.shipment_ids)

        stolen = self.url_open(
            '/my/orders/%s/shipments/create' % other.id,
            data=self._shipment_post(self._csrf(form.text)),
        )
        self.assertEqual(stolen.status_code, 200)
        self.env.invalidate_all()
        self.assertEqual(len(other.shipment_ids), before)
        self.assertFalse(self._orders_of(self.seller))
