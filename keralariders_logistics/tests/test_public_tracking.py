"""Public /track accepts KeralaXpress AWB and India Post article numbers.

Hermetic: no India Post HTTP, no login required, no live AWB consumption.
"""

from odoo.tests import HttpCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

ARTICLE = 'EY547000001IN'
ARTICLE_UNKNOWN = 'EY547000099IN'


@tagged('post_install', '-at_install')
class TestPublicTracking(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Public Track Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})
        cls.shipment = cls.env['logistics.shipment'].create({
            'seller_id': cls.seller.id,
            'shipping_to_name': 'Public Track Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Public track article',
            'total_weight': 0.95,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        })
        cls.shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
        })
        cls.awb = cls.shipment.name
        cls.token = cls.shipment.tracking_token

    def _assert_tracking_page(self, response):
        self.assertEqual(response.status_code, 200)
        self.assertIn(self.token, response.url)
        self.assertIn(self.awb, response.text)
        self.assertNotIn('No shipment found', response.text)
        self.assertNotIn('/web/login', response.url)

    def test_get_id_article_finds_shipment(self):
        response = self.url_open('/track?id=%s' % ARTICLE)
        self._assert_tracking_page(response)

    def test_get_id_article_is_case_insensitive(self):
        response = self.url_open('/track?id=%s' % ARTICLE.lower())
        self._assert_tracking_page(response)

    def test_post_article_finds_shipment(self):
        response = self.url_open('/track', data={'awb': ARTICLE})
        self._assert_tracking_page(response)

    def test_unknown_article_is_not_found_without_awb_only_copy(self):
        response = self.url_open('/track?id=%s' % ARTICLE_UNKNOWN)
        self.assertEqual(response.status_code, 200)
        self.assertIn('No shipment found', response.text)
        self.assertIn('India Post article number', response.text)
        self.assertNotIn('provided AWB Number.', response.text)
        self.assertIn(ARTICLE_UNKNOWN, response.text)
        self.assertNotIn(self.awb, response.text)

    def test_awb_still_works(self):
        response = self.url_open('/track?id=%s' % self.awb)
        self._assert_tracking_page(response)

    def test_uuid_token_url_still_works(self):
        response = self.url_open('/track/%s' % self.token)
        self._assert_tracking_page(response)

    def test_search_page_mentions_article_number(self):
        response = self.url_open('/track')
        self.assertEqual(response.status_code, 200)
        self.assertIn('AWB or India Post article number', response.text)
        self.assertIn('EY547878418IN', response.text)
