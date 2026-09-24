"""India Post booking/label sender name carries a KeralaXpress brand prefix.

The prefix is outbound-only (process-articles + /v1/label/create/domestic).
Our AWB / 100x150 QWeb and the seller record stay unprefixed.
"""
from pathlib import Path

from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models.indiapost_shipment import (
    IP_SENDER_BRAND_PREFIX,
)
from odoo.addons.keralariders_logistics.tests.common import (
    IndiapostHermeticMixin,
)

SELLER_NAME = 'Brand Prefix Seller'


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
            'street': '12 Brand Street',
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
            'shipping_to_name': 'Brand Customer',
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

    def test_booking_sender_name_is_branded(self):
        shipment = self._new_shipment(self.ip_seller)
        article = shipment._ip_prepare_article(self.settings, 'TT900000101IN')
        expected = IP_SENDER_BRAND_PREFIX + SELLER_NAME
        self.assertEqual(article['sender_name'], expected)
        self.assertEqual(article['sender_company'], expected)
        self.assertEqual(article['pickup_addressee_name'], SELLER_NAME)
        self.assertEqual(article['alt_addressee_name'], SELLER_NAME)

    def test_label_payload_sender_name_is_branded(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({'indiapost_article_number': 'TT900000102IN'})
        payload = shipment._ip_label_payload(self.settings)
        self.assertEqual(
            payload['sender_name'], IP_SENDER_BRAND_PREFIX + SELLER_NAME)

    def test_prepare_twice_does_not_double_prefix(self):
        shipment = self._new_shipment(self.ip_seller)
        first = shipment._ip_prepare_article(self.settings, 'TT900000103IN')
        second = shipment._ip_prepare_article(self.settings, 'TT900000104IN')
        expected = IP_SENDER_BRAND_PREFIX + SELLER_NAME
        self.assertEqual(first['sender_name'], expected)
        self.assertEqual(second['sender_name'], expected)
        self.assertEqual(
            second['sender_name'].count(IP_SENDER_BRAND_PREFIX.strip()), 1)
        # Helper is also idempotent when fed an already-branded string.
        again = shipment._ip_branded_sender_name(expected)
        self.assertEqual(again, expected)

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
        self.assertFalse(addr['name'].startswith(IP_SENDER_BRAND_PREFIX))
        self.assertEqual(shipment.shipping_from_name, SELLER_NAME)
        self.assertEqual(self.ip_seller.name, SELLER_NAME)

        report_dir = Path(__file__).resolve().parents[1] / 'report'
        for filename in (
            'shipment_layout.xml',
            'shipment_label_100x150.xml',
        ):
            source = (report_dir / filename).read_text(encoding='utf-8')
            self.assertNotIn(IP_SENDER_BRAND_PREFIX, source)
            self.assertNotIn('KeralaXpress]', source)
            self.assertIn("seller_addr['name']", source)
