"""Invalid destination PINs must warn sellers, not traceback or persist.

656875 is format-valid (six digits, southern region) but India Post has no
office for it. Tariff returns ``Destination pincode … not found``; the portal
must flash a pink warning and leave no order / shipment / wallet debit.
"""

import re
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.models.indiapost_client import (
    IndiapostApiError,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin
from odoo.addons.keralariders_logistics.tests.test_indiapost_tariff import (
    SP_PAYLOAD,
)

INVALID_PIN = '656875'
PIN_NOT_FOUND = 'Destination pincode %s not found' % INVALID_PIN

# Enough for hybrid resolve to invent a district so create reaches the tariff.
INVALID_PIN_OFFICES = [{
    'office_id': '65687501',
    'office_name': 'Fake Invalid SO',
    'office_type_code': 'SPO',
    'state_name': 'KERALA',
    'city_name': 'ERNAKULAM',
    'taluk_name': 'Ernakulam',
    'village_name': 'Fake',
    'delivery_office_flag': True,
    'is_rolled_out': True,
}]


def _response(payload):
    return type('FakeTariffResponse', (), {'payload': payload})()


@tagged('post_install', '-at_install')
class TestPortalInvalidPincode(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)

        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Invalid PIN Portal Seller',
            'zip': '682001',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.ip_login = 'kx_invalid_pin_portal'
        cls.env['res.users'].create({
            'name': 'Invalid PIN Portal',
            'login': cls.ip_login,
            'password': cls.ip_login,
            'partner_id': cls.ip_seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        cls.OfficeModel = cls.registry['logistics.indiapost.office']
        cls.ClientModel = cls.registry['logistics.indiapost.client']

    def _csrf(self, html):
        match = re.search(
            r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html)
        self.assertTrue(match, 'no csrf_token in the rendered page')
        return match.group(1)

    def _shipment_post(self, csrf, **overrides):
        data = {
            'csrf_token': csrf,
            'shipping_to_name': 'Invalid Pin Customer',
            'shipping_to_mobile': '9876543210',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': INVALID_PIN,
            'item_description': 'Toys',
            'total_weight': '1.5',
            'order_payment_type': 'prepaid',
            'pickup_date': fields.Date.context_today(self.env.user).isoformat(),
            'length_cm': '30',
            'breadth_cm': '20',
            'height_cm': '15',
        }
        data.update(overrides)
        return data

    def _orders_of(self, seller):
        return self.env['logistics.order'].search([('seller_id', '=', seller.id)])

    def test_tariff_pincode_not_found_flashes_warning_no_order(self):
        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/orders/manual')
        self.assertEqual(form.status_code, 200)
        csrf = self._csrf(form.text)
        before_orders = len(self._orders_of(self.ip_seller))
        before_shipments = self.env['logistics.shipment'].search_count([
            ('seller_id', '=', self.ip_seller.id),
        ])

        with patch.object(
            self.OfficeModel, '_ip_fetch_offices',
            return_value=INVALID_PIN_OFFICES,
        ), patch.object(
            self.ClientModel, 'call',
            side_effect=IndiapostApiError(PIN_NOT_FOUND),
        ):
            response = self.url_open(
                '/my/orders/create',
                data=self._shipment_post(csrf),
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn('/my/orders/manual', response.url)
        self.assertIn(
            '%s is not a valid delivery pincode' % INVALID_PIN,
            response.text,
        )
        self.assertIn('alert-danger', response.text)
        self.env.invalidate_all()
        self.assertEqual(len(self._orders_of(self.ip_seller)), before_orders)
        self.assertEqual(
            self.env['logistics.shipment'].search_count([
                ('seller_id', '=', self.ip_seller.id),
            ]),
            before_shipments,
        )

    def test_valid_quote_path_still_creates_order(self):
        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/orders/manual')
        csrf = self._csrf(form.text)

        def fake_call(this, method, path, params=None, **kwargs):
            return _response(SP_PAYLOAD)

        with patch.object(self.ClientModel, 'call', fake_call):
            response = self.url_open(
                '/my/orders/create',
                data=self._shipment_post(
                    csrf,
                    shipping_to_zip='695001',
                ),
            )

        self.env.invalidate_all()
        orders = self._orders_of(self.ip_seller)
        self.assertEqual(len(orders), 1)
        self.assertIn('/my/orders/%s' % orders.id, response.url)
        shipment = orders.shipment_ids
        self.assertEqual(len(shipment), 1)
        self.assertEqual(shipment.shipping_to_zip, '695001')
        self.assertFalse(shipment.indiapost_needs_quote)
        self.assertGreater(shipment.delivery_charges_total, 0)


@tagged('post_install', '-at_install')
class TestIndiapostQuoteInvalidPincode(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Invalid PIN Quote Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})
        district = cls.env['logistics.district'].search([
            ('name', 'ilike', 'Ernakulam'),
        ], limit=1)
        cls.shipment = cls.env['logistics.shipment'].create({
            'seller_id': cls.seller.id,
            'shipping_to_name': 'Quote Customer',
            'shipping_to_mobile': '9876543210',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': INVALID_PIN,
            'shipping_to_district_id': district.id if district else False,
            'item_description': 'Toys',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
            'state': 'order_added',
            **cls.env['logistics.shipment']._shipping_from_vals_for_seller(
                cls.seller),
        })

    def test_action_indiapost_quote_raises_user_error_not_api_error(self):
        with patch.object(
            self.registry['logistics.indiapost.client'], 'call',
            side_effect=IndiapostApiError(PIN_NOT_FOUND),
        ):
            with self.assertRaises(UserError) as err:
                self.shipment.action_indiapost_quote()
        self.assertNotIsInstance(err.exception, IndiapostApiError)
        self.assertIn(
            '%s is not a valid delivery pincode' % INVALID_PIN,
            str(err.exception),
        )

    def test_tariff_quote_raises_user_error_for_pincode_not_found(self):
        with patch.object(
            self.registry['logistics.indiapost.client'], 'call',
            side_effect=IndiapostApiError(PIN_NOT_FOUND),
        ):
            with self.assertRaises(UserError) as err:
                self.env['logistics.indiapost.tariff'].quote(
                    '682001', INVALID_PIN, weight_g=1500,
                    length_cm=30, breadth_cm=20, height_cm=15,
                    use_cache=False,
                )
        self.assertIn(
            '%s is not a valid delivery pincode' % INVALID_PIN,
            str(err.exception),
        )
