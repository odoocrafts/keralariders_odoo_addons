"""India Post pickup/booking must collect from the seller, not the company.

The official CEPT label prints SENDER from booking ``sender_*``. Pickup lives
in the same article (there is no standalone pickup HTTP API). KeralaXpress
stays the bulk customer / contract holder; the physical from-address does not.
"""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import (
    SP_CONTRACT,
    IndiapostHermeticMixin,
)

SELLER_NAME = 'Alleppey Spice House'
SELLER_STREET = '12 Beach Road Cullan'
SELLER_STREET2 = 'Near Boat Jetty'
SELLER_MOBILE = '9847011111'
SELLER_PIN = '682001'

COMPANY_STREET = 'KeralaXpress Warehouse NH66'
COMPANY_LANDLINE = '04871234567'
COMPANY_MOBILE = '9400662693'
COMPANY_NAME = 'KERALA XPRESS LOGISTICS'
SETTINGS_STREET = 'Vazhiyambalam, Bypass NH66'


@tagged('post_install', '-at_install')
class TestIndiapostPickupAddress(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': SELLER_NAME,
            'zip': SELLER_PIN,
            'phone': SELLER_MOBILE,
            'street': SELLER_STREET,
            'street2': SELLER_STREET2,
            'city': 'Alappuzha',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})

    def setUp(self):
        super().setUp()
        self._ip_enable_stub_credentials()
        partner = self.env.company.partner_id
        partner.write({
            'name': COMPANY_NAME,
            'street': COMPANY_STREET,
            'street2': 'Vazhiyambalam Bypass',
            'city': 'Thrissur',
            'zip': '680681',
            'phone': COMPANY_LANDLINE,
        })
        if 'mobile' in partner._fields:
            partner.write({'mobile': COMPANY_MOBILE})
        self.settings = self.env['logistics.indiapost.client']._ip_settings()

    def _new_shipment(self, **overrides):
        vals = {
            'seller_id': self.seller.id,
            'shipping_to_name': 'Pickup Test Customer',
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

    def _company_leaks(self, blob):
        haystack = ' '.join(str(value) for value in blob)
        return [
            token for token in (
                COMPANY_STREET,
                COMPANY_LANDLINE,
                COMPANY_NAME,
                SETTINGS_STREET,
            )
            if token in haystack
        ]

    def _pickup_and_sender_values(self, article):
        return {
            'pickup_addressee_name': article.get('pickup_addressee_name'),
            'pickup_company_name': article.get('pickup_company_name'),
            'pickup_address_line1': article.get('pickup_address_line1'),
            'pickup_address_line2': article.get('pickup_address_line2'),
            'pickup_city': article.get('pickup_city'),
            'pickup_state': article.get('pickup_state'),
            'pickup_pincode': article.get('pickup_pincode'),
            'pickup_mobile_no': article.get('pickup_mobile_no'),
            'sender_name': article.get('sender_name'),
            'sender_company': article.get('sender_company'),
            'sender_add_line_1': article.get('sender_add_line_1'),
            'sender_add_line_2': article.get('sender_add_line_2'),
            'sender_city': article.get('sender_city'),
            'sender_state': article.get('sender_state'),
            'sender_pincode': article.get('sender_pincode'),
            'sender_mobile_no': article.get('sender_mobile_no'),
            'alt_addressee_name': article.get('alt_addressee_name'),
            'alt_address_line1': article.get('alt_address_line1'),
            'alt_alternate_mobile_no': article.get('alt_alternate_mobile_no'),
        }

    def test_booking_article_uses_seller_not_company_for_pickup(self):
        """Pickup, sender, and return-to-seller are the seller's premises."""
        shipment = self._new_shipment()
        article = shipment._ip_prepare_article(self.settings, 'TT900000016IN')
        values = self._pickup_and_sender_values(article)

        self.assertEqual(article['pickup_address_flag'], 'TRUE')
        self.assertEqual(values['pickup_addressee_name'], SELLER_NAME)
        self.assertEqual(values['pickup_address_line1'], SELLER_STREET)
        self.assertEqual(values['pickup_address_line2'], SELLER_STREET2)
        self.assertEqual(values['pickup_pincode'], SELLER_PIN)
        self.assertEqual(values['pickup_mobile_no'], SELLER_MOBILE)

        self.assertEqual(values['sender_name'], SELLER_NAME)
        self.assertEqual(values['sender_add_line_1'], SELLER_STREET)
        self.assertEqual(values['sender_add_line_2'], SELLER_STREET2)
        self.assertEqual(values['sender_pincode'], SELLER_PIN)
        self.assertEqual(values['sender_mobile_no'], SELLER_MOBILE)

        self.assertEqual(values['alt_addressee_name'], SELLER_NAME)
        self.assertEqual(values['alt_address_line1'], SELLER_STREET)
        self.assertEqual(values['alt_alternate_mobile_no'], SELLER_MOBILE)

        self.assertEqual(article['bulk_customer_id'],
                         self.settings['indiapost_customer_id'])
        self.assertEqual(article['contract_id'], SP_CONTRACT)
        self.assertFalse(self._company_leaks(values.values()))
        self.assertNotIn(COMPANY_MOBILE, values.values())

    def test_booking_http_body_sends_seller_pickup(self):
        """The dict actually posted to process-articles carries the seller."""
        shipment = self._new_shipment()
        captured = []

        def capture_call(*args, **kwargs):
            captured.append((args, kwargs))
            return self._ip_stub_call(*args, **kwargs)

        with self._ip_patch_call(side_effect=capture_call):
            shipment.action_indiapost_book()

        booking = None
        for args, kwargs in captured:
            path = kwargs.get('path')
            if path is None and len(args) >= 3:
                path = args[2]
            body = kwargs.get('body')
            if body is None:
                for arg in args:
                    if isinstance(arg, dict) and 'articles' in arg:
                        body = arg
                        break
            if body and 'articles' in body:
                booking = body
                break
        self.assertIsNotNone(booking, 'No booking payload was posted')
        article = booking['articles'][0]
        self.assertEqual(article['pickup_address_line1'], SELLER_STREET)
        self.assertEqual(article['pickup_mobile_no'], SELLER_MOBILE)
        self.assertEqual(article['sender_add_line_1'], SELLER_STREET)
        self.assertEqual(article['sender_mobile_no'], SELLER_MOBILE)
        self.assertNotEqual(article['sender_name'], COMPANY_NAME)
        self.assertFalse(self._company_leaks(
            self._pickup_and_sender_values(article).values()))

    def test_shipping_from_is_the_pickup_source_of_truth(self):
        """Portal pickup address on the shipment wins over seller.street."""
        shipment = self._new_shipment()
        shipment.write({
            'shipping_from_name': 'Portal Pickup Contact',
            'shipping_from_address': '99 Portal Pickup Lane\nFirst Floor',
        })
        article = shipment._ip_prepare_article(self.settings, 'TT900000024IN')
        self.assertEqual(article['pickup_addressee_name'], 'Portal Pickup Contact')
        self.assertEqual(article['pickup_address_line1'], '99 Portal Pickup Lane')
        self.assertEqual(article['pickup_address_line2'], 'First Floor')
        self.assertEqual(article['sender_name'], 'Portal Pickup Contact')
        self.assertEqual(article['sender_add_line_1'], '99 Portal Pickup Lane')
        self.assertEqual(article['pickup_mobile_no'], SELLER_MOBILE)

    def test_partner_phone_is_used_when_seller_phone_is_blank(self):
        """Partner phone is the stored seller mobile when the related field is empty."""
        partner_mobile = '9876500001'
        self.seller.partner_id.write({'phone': partner_mobile})
        # Related ``seller.phone`` normally tracks partner.phone; clear the
        # cache so a stale related value cannot hide the partner write.
        self.seller.invalidate_recordset(['phone'])
        article = self._new_shipment()._ip_prepare_article(
            self.settings, 'TT900000032IN')
        self.assertEqual(article['pickup_mobile_no'], partner_mobile)
        self.assertEqual(article['sender_mobile_no'], partner_mobile)

    def test_missing_seller_mobile_raises_user_error(self):
        self.seller.phone = False
        self.seller.partner_id.write({'phone': False})
        shipment = self._new_shipment()
        with self.assertRaises(UserError) as caught:
            shipment._ip_prepare_article(self.settings, 'TT900000040IN')
        self.assertIn('mobile', str(caught.exception).lower())

    def test_missing_pickup_pincode_raises_user_error(self):
        shipment = self._new_shipment()
        shipment.write({'shipping_from_zip': ''})
        self.seller.zip = False
        if self.seller.partner_id:
            self.seller.partner_id.zip = False
        with self.assertRaises(UserError) as caught:
            shipment._ip_origin_pincode()
        self.assertIn('pincode', str(caught.exception).lower())
