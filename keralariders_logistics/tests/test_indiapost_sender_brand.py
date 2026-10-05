"""India Post booking/label sender names and addresses carry a brand prefix.

The prefix is outbound-only on every sender-side name and address line
(process-articles + /v1/label/create/domestic): sender, pickup, and alt.
Seller API pickup uses that same article builder. Receiver fields, phones,
standalone city/state, our AWB / 100x150 QWeb, and the seller record stay
unprefixed.
"""
from pathlib import Path

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.models.indiapost_shipment import (
    IP_SENDER_BRAND_PREFIX,
)
from odoo.addons.keralariders_logistics.tests.common import (
    IndiapostHermeticMixin,
)

SELLER_NAME = 'Brand Prefix Seller'
SELLER_STREET = '12 Brand Street'
RECEIVER_NAME = 'Brand Customer'
RECEIVER_ADDRESS = '12 Test Road, Test Nagar'
RECEIVER_MOBILE = '9876543210'
SELLER_MOBILE = '9847011111'


@tagged('post_install', '-at_install')
class TestIndiapostSenderBrand(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': SELLER_NAME,
            'zip': '682001',
            'phone': '9847011111',
            'street': SELLER_STREET,
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.hub_seller = cls.env['logistics.seller'].create({
            'name': 'Own Network Seller',
            'zip': '682001',
            'phone': '9847022222',
            'street': 'Hub Brand Street',
        })
        cls.hub_seller.write({'fulfilment_method': 'own_network'})

    def setUp(self):
        super().setUp()
        self._ip_enable_stub_credentials()
        self.settings = self.env['logistics.indiapost.client']._ip_settings()

    def _new_shipment(self, seller, **overrides):
        vals = {
            'seller_id': seller.id,
            'shipping_to_name': RECEIVER_NAME,
            'shipping_to_address': RECEIVER_ADDRESS,
            'shipping_to_zip': '695001',
            'shipping_to_mobile': RECEIVER_MOBILE,
            'item_description': 'Test article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        }
        vals.update(overrides)
        return self.env['logistics.shipment'].create(vals)

    def _assert_sender_branded(self, article, name=SELLER_NAME, street=SELLER_STREET):
        expected_name = IP_SENDER_BRAND_PREFIX + name
        expected_street = IP_SENDER_BRAND_PREFIX + street
        for key in (
            'sender_name', 'sender_company',
            'pickup_addressee_name', 'pickup_company_name',
            'alt_addressee_name', 'alt_company_name',
        ):
            self.assertEqual(article[key], expected_name)
            self.assertEqual(article[key].count(IP_SENDER_BRAND_PREFIX), 1)
        for key in (
            'sender_add_line_1',
            'pickup_address_line1',
            'alt_address_line1',
        ):
            self.assertEqual(article[key], expected_street)
            self.assertEqual(article[key].count(IP_SENDER_BRAND_PREFIX), 1)
        self.assertEqual(article['receiver_name'], RECEIVER_NAME)
        self.assertEqual(article['receiver_company'], RECEIVER_NAME)
        self.assertEqual(article['receiver_add_line_1'], RECEIVER_ADDRESS)
        self.assertEqual(article['receiver_mobile_no'], RECEIVER_MOBILE)
        self.assertEqual(article['sender_mobile_no'], SELLER_MOBILE)
        self.assertEqual(article['pickup_mobile_no'], SELLER_MOBILE)
        self.assertEqual(article['alt_alternate_mobile_no'], SELLER_MOBILE)
        for key in (
            'receiver_name', 'receiver_company', 'receiver_add_line_1',
            'sender_city', 'pickup_city', 'alt_city',
            'sender_state', 'pickup_state', 'alt_state',
        ):
            self.assertFalse(
                str(article.get(key) or '').startswith(IP_SENDER_BRAND_PREFIX),
                key,
            )

    def test_booking_sender_name_and_address_are_branded(self):
        shipment = self._new_shipment(self.ip_seller)
        article = shipment._ip_prepare_article(self.settings, 'TT900000101IN')
        self._assert_sender_branded(article)

    def test_label_payload_sender_name_and_address_are_branded(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({'indiapost_article_number': 'TT900000102IN'})
        payload = shipment._ip_label_payload(self.settings)
        self.assertEqual(
            payload['sender_name'], IP_SENDER_BRAND_PREFIX + SELLER_NAME)
        self.assertEqual(
            payload['sender_addressl1'], IP_SENDER_BRAND_PREFIX + SELLER_STREET)
        self.assertEqual(
            payload['sender_addressl1'].count(IP_SENDER_BRAND_PREFIX), 1)
        self.assertEqual(payload['recipient_name'], RECEIVER_NAME)
        self.assertEqual(payload['recipient_addressl1'], RECEIVER_ADDRESS)
        self.assertEqual(payload['recipient_mobile'], RECEIVER_MOBILE)
        self.assertEqual(payload['sender_mobile'], SELLER_MOBILE)
        self.assertFalse(
            payload['sender_city'].startswith(IP_SENDER_BRAND_PREFIX))
        self.assertFalse(
            payload['recipient_name'].startswith(IP_SENDER_BRAND_PREFIX))
        self.assertFalse(
            payload['recipient_addressl1'].startswith(IP_SENDER_BRAND_PREFIX))

    def test_prepare_twice_does_not_double_prefix(self):
        shipment = self._new_shipment(self.ip_seller)
        first = shipment._ip_prepare_article(self.settings, 'TT900000103IN')
        second = shipment._ip_prepare_article(self.settings, 'TT900000104IN')
        expected_name = IP_SENDER_BRAND_PREFIX + SELLER_NAME
        expected_street = IP_SENDER_BRAND_PREFIX + SELLER_STREET
        for key in (
            'sender_name', 'sender_company',
            'pickup_addressee_name', 'pickup_company_name',
            'alt_addressee_name', 'alt_company_name',
        ):
            self.assertEqual(first[key], expected_name)
            self.assertEqual(second[key], expected_name)
            self.assertEqual(second[key].count(IP_SENDER_BRAND_PREFIX), 1)
        for key in (
            'sender_add_line_1',
            'pickup_address_line1',
            'alt_address_line1',
        ):
            self.assertEqual(first[key], expected_street)
            self.assertEqual(second[key], expected_street)
            self.assertEqual(second[key].count(IP_SENDER_BRAND_PREFIX), 1)
        # Helper is also idempotent when fed an already-branded string.
        again = shipment._ip_branded_sender_name(expected_name)
        self.assertEqual(again, expected_name)
        again_street = shipment._ip_branded_sender_name(expected_street)
        self.assertEqual(again_street, expected_street)

    def test_address_prefix_counts_toward_field_limit(self):
        street = 'A' * ipc.TEXT_MAX_LEN
        shipment = self._new_shipment(self.ip_seller)
        shipment.write({'shipping_from_address': street})
        article = shipment._ip_prepare_article(self.settings, 'TT900000105IN')
        branded = (IP_SENDER_BRAND_PREFIX + street)[:ipc.TEXT_MAX_LEN]
        self.assertTrue(branded.startswith(IP_SENDER_BRAND_PREFIX))
        self.assertEqual(len(branded), ipc.TEXT_MAX_LEN)
        self.assertLess(len(branded) - len(IP_SENDER_BRAND_PREFIX), len(street))
        for key in (
            'sender_add_line_1',
            'pickup_address_line1',
            'alt_address_line1',
        ):
            self.assertEqual(article[key], branded)
            self.assertEqual(article[key].count(IP_SENDER_BRAND_PREFIX), 1)
        self.assertEqual(shipment._ip_branded_sender_name(branded), branded)

    def test_seller_api_booking_payload_brands_sender_name_and_address(self):
        """POST /api/v1/seller/shipments books via action_request_pickup.

        That method is the only seller-API booking entry. It posts the same
        ``_ip_prepare_article`` body as Request Pickup on the portal.
        """
        seller = self.env['logistics.seller'].create({
            'name': 'LAMART GROUP',
            'zip': '682001',
            'phone': SELLER_MOBILE,
            'street': 'Lamart Warehouse Road',
            'street2': 'Near Tirur HO',
        })
        seller.write({'fulfilment_method': 'indiapost'})
        wallet = seller.wallet_ids[0]
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': wallet.id,
            'amount': 5000.0,
            'reference': 'Brand test top-up',
        })
        order = self.env['logistics.order'].create({'seller_id': seller.id})
        shipment = self._new_shipment(seller, order_id=order.id)
        shipment.write({
            'indiapost_base_tariff': 100.0,
            'indiapost_vas_charges': 0.0,
            'indiapost_tax_amount': 18.0,
            'indiapost_total_tariff': 118.0,
            'indiapost_quoted_weight_g': ipc.band_weight(
                ipc.kg_to_grams(shipment.total_weight)),
            'indiapost_tariff_quoted_on': fields.Datetime.now(),
            'indiapost_quote_signature': shipment._ip_quote_signature(),
        })
        captured = []

        def capture_call(*args, **kwargs):
            captured.append((args, kwargs))
            return self._ip_stub_call(*args, **kwargs)

        with self._ip_patch_call(side_effect=capture_call):
            order.action_request_pickup()

        booking = None
        for args, kwargs in captured:
            body = kwargs.get('body')
            if body is None:
                for arg in args:
                    if isinstance(arg, dict) and 'articles' in arg:
                        body = arg
                        break
            if body and 'articles' in body:
                booking = body
                break
        self.assertIsNotNone(booking, 'Seller API pickup posted no article')
        article = booking['articles'][0]
        self._assert_sender_branded(
            article, name='LAMART GROUP', street='Lamart Warehouse Road')
        second_line = IP_SENDER_BRAND_PREFIX + 'Near Tirur HO'
        self.assertEqual(article['sender_add_line_2'], second_line)
        self.assertEqual(article['pickup_address_line2'], second_line)
        self.assertEqual(article['alt_address_line2'], second_line)
        self.assertEqual(article['sender_add_line_2'].count(IP_SENDER_BRAND_PREFIX), 1)
        self.assertEqual(shipment.seller_id.name, 'LAMART GROUP')
        self.assertFalse(shipment.shipping_from_address.startswith(
            IP_SENDER_BRAND_PREFIX))
        again = shipment._ip_prepare_article(self.settings, 'TT900000106IN')
        self.assertEqual(again['sender_name'], article['sender_name'])
        self.assertEqual(again['sender_add_line_1'], article['sender_add_line_1'])
        self.assertEqual(again['sender_name'].count(IP_SENDER_BRAND_PREFIX), 1)
        self.assertEqual(
            again['sender_add_line_1'].count(IP_SENDER_BRAND_PREFIX), 1)

    def test_own_network_shipment_unaffected(self):
        shipment = self._new_shipment(self.hub_seller)
        self.assertEqual(shipment.fulfilment_method, 'own_network')
        addr = shipment._awb_seller_address()
        self.assertEqual(addr['name'], 'Own Network Seller')
        self.assertFalse(addr['name'].startswith(IP_SENDER_BRAND_PREFIX))
        self.assertEqual(self.hub_seller.name, 'Own Network Seller')

    def test_awb_and_label_qweb_use_unbranded_seller(self):
        shipment = self._new_shipment(self.ip_seller)
        addr = shipment._awb_seller_address()
        self.assertEqual(addr['name'], SELLER_NAME)
        self.assertEqual(addr['street'], SELLER_STREET)
        self.assertFalse(addr['name'].startswith(IP_SENDER_BRAND_PREFIX))
        self.assertFalse(addr['street'].startswith(IP_SENDER_BRAND_PREFIX))
        self.assertEqual(shipment.shipping_from_name, SELLER_NAME)
        self.assertEqual(self.ip_seller.name, SELLER_NAME)
        self.assertNotIn(IP_SENDER_BRAND_PREFIX, shipment.shipping_from_address or '')

        report_dir = Path(__file__).resolve().parents[1] / 'report'
        for filename in (
            'shipment_layout.xml',
            'shipment_label_100x150.xml',
        ):
            source = (report_dir / filename).read_text(encoding='utf-8')
            self.assertNotIn(IP_SENDER_BRAND_PREFIX, source)
            self.assertNotIn('KeralaXpress]', source)
            self.assertIn("seller_addr['name']", source)
