"""Portal charge calculator: Speed Post and Business Parcel share ``quote_safe``."""

from unittest.mock import patch

from odoo.tests import HttpCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


def _ok_quote(**overrides):
    quote = {
        'ok': True,
        'article_type': ipc.ARTICLE_TYPE_SPEED_POST,
        'article_type_label': 'Speed Post',
        'product_code': 'SP_INLAND_DOC',
        'source_pincode': '680681',
        'destination_pincode': '110001',
        'chargeable_weight_g': 250,
        'actual_weight_g': 250,
        'volumetric_weight_g': 252,
        'billed_weight_g': 250,
        'weight_banded': False,
        'is_local': False,
        'distance_display': '2038',
        'base_tariff': 77.0,
        'vas_charges': 0.0,
        'vas_details': {},
        'cgst': 7.0,
        'sgst': 7.0,
        'total_tax': 14.0,
        'final_amount': 91.0,
        'markup_amount': 0.0,
        'markup_percent': 0.0,
        'total_payable': 91.0,
        'insurance_value': 0.0,
        'warnings': [],
        'is_document': True,
    }
    quote.update(overrides)
    return quote


@tagged('post_install', '-at_install')
class TestPortalCalculator(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Calculator IP Seller',
            'zip': '680681',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.ip_login = 'kx_calculator_ip'
        cls.env['res.users'].create({
            'name': 'Calculator IP Portal',
            'login': cls.ip_login,
            'password': cls.ip_login,
            'partner_id': cls.ip_seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def _csrf(self, html):
        import re
        match = re.search(r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html)
        self.assertTrue(match, 'no csrf_token in the rendered page')
        return match.group(1)

    def test_calculator_offers_speed_post_and_business_parcel(self):
        self.authenticate(self.ip_login, self.ip_login)
        page = self.url_open('/my/calculator')
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="indiapost_article_type"', page.text)
        self.assertIn('Speed Post', page.text)
        self.assertIn('Business Parcel', page.text)
        self.assertIn('normal parcel', page.text)
        self.assertNotIn('India Post Speed Post', page.text)

    def test_calculator_posts_business_parcel_to_the_shared_quote_helper(self):
        captured = []

        def fake_quote_safe(this, *args, **kwargs):
            captured.append({'args': args, 'kwargs': kwargs})
            return _ok_quote(
                article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL,
                article_type_label='Business Parcel',
                product_code='BUSINESS_PARCEL',
                base_tariff=35.0,
                final_amount=41.0,
                total_payable=41.0,
                is_document=False,
            )

        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/calculator')
        with patch.object(self.registry['logistics.indiapost.tariff'],
                          'quote_safe', fake_quote_safe):
            result = self.url_open('/my/calculator/calculate', data={
                'csrf_token': self._csrf(form.text),
                'origin_pincode': '680681',
                'dest_pincode': '110001',
                'weight': '0.25',
                'length_cm': '30',
                'breadth_cm': '21',
                'height_cm': '2',
                'indiapost_article_type': 'BP',
            })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0]['kwargs']['article_type'],
            ipc.ARTICLE_TYPE_BUSINESS_PARCEL,
        )
        self.assertIn('Business Parcel', result.text)
        self.assertIn('35.00', result.text)

    def test_calculator_rejects_an_unknown_product_code(self):
        captured = []

        def fake_quote_safe(this, *args, **kwargs):
            captured.append(kwargs)
            return _ok_quote()

        self.authenticate(self.ip_login, self.ip_login)
        form = self.url_open('/my/calculator')
        with patch.object(self.registry['logistics.indiapost.tariff'],
                          'quote_safe', fake_quote_safe):
            self.url_open('/my/calculator/calculate', data={
                'csrf_token': self._csrf(form.text),
                'origin_pincode': '680681',
                'dest_pincode': '110001',
                'weight': '0.25',
                'length_cm': '30',
                'breadth_cm': '21',
                'height_cm': '2',
                'indiapost_article_type': 'HACK',
            })
        self.assertEqual(
            captured[0]['article_type'], ipc.ARTICLE_TYPE_SPEED_POST)
