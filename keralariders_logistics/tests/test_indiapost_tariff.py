"""Business Parcel and Speed Post share one quote helper, not two pricing paths.

Production prices Business Parcel on
``/v1/business-parcel-tariff/calculate``. The Speed Post URL still answers
HTTP 422 for product-code=BP. The response shape also differs: Speed Post
sends ``vas_charges`` as a number plus ``vas_details``, Business Parcel sends
the VAS lines as ``vas_charges`` itself. Both have to come out of ``quote()``
looking the same, or the calculator and the shipment charge card fork.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.models.indiapost_tariff import (
    BUSINESS_PARCEL_TARIFF_PATH,
    SPEED_POST_TARIFF_PATH,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


def _response(payload):
    return type('FakeTariffResponse', (), {'payload': payload})()


SP_PAYLOAD = {
    'success': True,
    'product_code': 'SP_INLAND_PARCEL',
    'chargeable_weight': 1800,
    'is_local': False,
    'distance_km': 'OS',
    'base_tariff': 190,
    'vas_charges': 68,
    'vas_details': {'INSVAL': 58, 'PODVAL': 10},
    'cgst': 23,
    'sgst': 23,
    'total_tax': 46,
    'final_amount': 304,
    'delivery_type': 'Inter-city',
}

BP_PAYLOAD = {
    'success': True,
    'product_code': 'BUSINESS_PARCEL',
    'article_type': 'Business Parcel',
    'chargeable_weight': 1800,
    'is_local': False,
    'distance_km': 0,
    'base_tariff': 115,
    'vas_charges': {'INSVAL': 58, 'REGVAL': 5, 'OTPVAL': 5},
    'cod_charges': 0,
    'cgst': 16,
    'sgst': 16,
    'total_tax': 32,
    'final_amount': 215,
    'delivery_type': 'Inter-city',
}


@tagged('post_install', '-at_install')
class TestIndiapostTariff(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)

    def setUp(self):
        super().setUp()
        self._ip_enable_stub_credentials()
        self.Tariff = self.env['logistics.indiapost.tariff']

    def test_normalize_rewrites_business_parcel_vas_dict(self):
        normalized = self.Tariff._ip_normalize_tariff_payload(BP_PAYLOAD)
        self.assertEqual(normalized['vas_charges'], 68.0)
        self.assertEqual(normalized['vas_details']['INSVAL'], 58)
        self.assertEqual(normalized['vas_details']['REGVAL'], 5)
        self.assertEqual(normalized['base_tariff'], 115)

    def test_normalize_leaves_speed_post_shape_alone(self):
        normalized = self.Tariff._ip_normalize_tariff_payload(SP_PAYLOAD)
        self.assertEqual(normalized['vas_charges'], 68)
        self.assertEqual(normalized['vas_details']['PODVAL'], 10)

    def test_quote_sends_business_parcel_to_the_dedicated_path(self):
        captured = []

        def fake_call(this, method, path, params=None, **kwargs):
            captured.append({'method': method, 'path': path, 'params': params})
            return _response(BP_PAYLOAD)

        with patch.object(self.registry['logistics.indiapost.client'],
                          'call', fake_call):
            quote = self.Tariff.quote(
                '680681', '110001', weight_g=1000, length_cm=30,
                breadth_cm=20, height_cm=15, insurance_value=1000,
                article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL, use_cache=False,
            )

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['method'], 'GET')
        self.assertEqual(captured[0]['path'], BUSINESS_PARCEL_TARIFF_PATH)
        self.assertEqual(captured[0]['params']['product-code'],
                         ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(quote['article_type'], ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(quote['article_type_label'], 'Business Parcel')
        self.assertEqual(quote['product_code'], 'BUSINESS_PARCEL')
        self.assertEqual(quote['base_tariff'], 115)
        self.assertEqual(quote['vas_charges'], 68.0)
        self.assertEqual(quote['vas_details']['INSVAL'], 58)
        self.assertEqual(quote['final_amount'], 215)
        self.assertFalse(quote['is_document'])

    def test_quote_keeps_speed_post_on_the_original_path(self):
        captured = []

        def fake_call(this, method, path, params=None, **kwargs):
            captured.append({'method': method, 'path': path, 'params': params})
            return _response(SP_PAYLOAD)

        with patch.object(self.registry['logistics.indiapost.client'],
                          'call', fake_call):
            quote = self.Tariff.quote(
                '680681', '110001', weight_g=1000, length_cm=30,
                breadth_cm=20, height_cm=15, use_cache=False,
            )

        self.assertEqual(captured[0]['path'], SPEED_POST_TARIFF_PATH)
        self.assertEqual(captured[0]['params']['product-code'],
                         ipc.ARTICLE_TYPE_SPEED_POST)
        self.assertEqual(quote['article_type'], ipc.ARTICLE_TYPE_SPEED_POST)
        self.assertEqual(quote['vas_charges'], 68)
        self.assertEqual(quote['final_amount'], 304)
