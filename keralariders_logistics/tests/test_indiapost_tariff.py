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
                         ipc.PRODUCT_PARCEL)
        self.assertEqual(quote['article_type'], ipc.ARTICLE_TYPE_SPEED_POST)
        self.assertEqual(quote['vas_charges'], 68)
        self.assertEqual(quote['final_amount'], 304)

    def test_quote_requests_inland_parcel_at_500_g(self):
        """500 g Speed Post stays on Speed Post inland parcel (not document)."""
        captured = []

        def fake_call(this, method, path, params=None, **kwargs):
            captured.append({'method': method, 'path': path, 'params': params})
            return _response(SP_PAYLOAD)

        with patch.object(self.registry['logistics.indiapost.client'],
                          'call', fake_call):
            quote = self.Tariff.quote(
                '676552', '683544', weight_g=500, length_cm=14,
                breadth_cm=9, height_cm=1, use_cache=False,
            )

        self.assertEqual(captured[0]['method'], 'GET')
        self.assertEqual(captured[0]['path'], SPEED_POST_TARIFF_PATH)
        self.assertEqual(captured[0]['params']['product-code'],
                         ipc.PRODUCT_PARCEL)
        self.assertNotEqual(captured[0]['params']['product-code'],
                            ipc.ARTICLE_TYPE_SPEED_POST)
        self.assertEqual(captured[0]['params']['weight'], 500)
        self.assertFalse(quote['is_document'])
        self.assertEqual(quote['article_type'], ipc.ARTICLE_TYPE_SPEED_POST)

    def test_quote_redirects_speed_post_below_500_g_to_business_parcel(self):
        """499 g requested as Speed Post must use the Business Parcel path."""
        captured = []

        def fake_call(this, method, path, params=None, **kwargs):
            captured.append({'method': method, 'path': path, 'params': params})
            return _response(BP_PAYLOAD)

        with patch.object(self.registry['logistics.indiapost.client'],
                          'call', fake_call):
            quote = self.Tariff.quote(
                '676552', '683544', weight_g=499, length_cm=14,
                breadth_cm=9, height_cm=1,
                article_type=ipc.ARTICLE_TYPE_SPEED_POST, use_cache=False,
            )

        self.assertEqual(captured[0]['path'], BUSINESS_PARCEL_TARIFF_PATH)
        self.assertEqual(captured[0]['params']['product-code'],
                         ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(quote['article_type'], ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(quote['article_type_label'], 'Business Parcel')

    def test_quote_keeps_explicit_business_parcel_below_500_g(self):
        captured = []

        def fake_call(this, method, path, params=None, **kwargs):
            captured.append({'path': path, 'params': params})
            return _response(BP_PAYLOAD)

        with patch.object(self.registry['logistics.indiapost.client'],
                          'call', fake_call):
            quote = self.Tariff.quote(
                '676552', '683544', weight_g=200, length_cm=14,
                breadth_cm=9, height_cm=1,
                article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL, use_cache=False,
            )

        self.assertEqual(captured[0]['path'], BUSINESS_PARCEL_TARIFF_PATH)
        self.assertEqual(quote['article_type'], ipc.ARTICLE_TYPE_BUSINESS_PARCEL)


def _lane_response(distance_km, base_tariff, total_tax, final_amount,
                   product_code='SP_INLAND_PARCEL', cgst=None, sgst=None):
    half = (total_tax / 2.0) if cgst is None else cgst
    other = (total_tax - half) if sgst is None else sgst
    return {
        'success': True,
        'product_code': product_code,
        'chargeable_weight': 2950,
        'is_local': False,
        'distance_km': distance_km,
        'base_tariff': base_tariff,
        'vas_charges': 0,
        'vas_details': {},
        'cgst': half,
        'sgst': other,
        'total_tax': total_tax,
        'final_amount': final_amount,
        'delivery_type': 'Inter-city',
    }


# Live 2950 g Speed Post from 685606 on 2026-10-01.
SP_WITHIN_STATE = _lane_response('WS', 111, 20, 131)
SP_ZONE_METRO = _lane_response('ZM', 190, 34, 224)
SP_OTHER_STATE = _lane_response('OS', 250, 46, 296)
# Business Parcel does not return a zone. 400 g from the same origin:
# Kochi / Lakshadweep direct 37, Chennai (ZM) 40, Mumbai (OS) 41.
BP_WITHIN_STATE = _lane_response(
    0, 31, 6, 37, product_code='BUSINESS_PARCEL', cgst=3, sgst=3)
BP_ZONE_METRO = _lane_response(
    0, 34, 6, 40, product_code='BUSINESS_PARCEL', cgst=3, sgst=3)


@tagged('post_install', '-at_install')
class TestLakshadweepMetroTariff(IndiapostHermeticMixin, TransactionCase):
    """682552 is Lakshadweep but the tariff API prices it as within Kerala."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)

    def setUp(self):
        super().setUp()
        self._ip_enable_stub_credentials()
        self.Tariff = self.env['logistics.indiapost.tariff']

    def _quote(self, destination, article_type=ipc.ARTICLE_TYPE_SPEED_POST,
               weight_g=2950, other_states=False):
        captured = []

        def fake_call(this, method, path, params=None, **kwargs):
            params = params or {}
            captured.append({'path': path, 'params': params})
            dest = params.get('destination-pincode')
            if 'business-parcel' in (path or ''):
                if dest == '600001':
                    return _response(BP_ZONE_METRO)
                return _response(BP_WITHIN_STATE)
            if dest == '600001':
                return _response(SP_ZONE_METRO)
            if dest == '682001':
                return _response(SP_WITHIN_STATE)
            if dest in ('682552', '682560') and other_states:
                return _response(SP_OTHER_STATE)
            return _response(SP_WITHIN_STATE)

        with patch.object(self.registry['logistics.indiapost.client'],
                          'call', fake_call):
            quote = self.Tariff.quote(
                '685606', destination, weight_g=weight_g, length_cm=43,
                breadth_cm=26, height_cm=6, article_type=article_type,
                use_cache=False,
            )
        return quote, captured

    def test_lakshadweep_speed_post_uses_zone_metro_not_within_state(self):
        quote, captured = self._quote('682552')
        self.assertEqual(quote['destination_pincode'], '682552')
        self.assertEqual(quote['distance_display'], 'ZM')
        self.assertEqual(quote['base_tariff'], 190)
        self.assertEqual(quote['total_tax'], 34)
        self.assertEqual(quote['final_amount'], 224)
        self.assertNotEqual(quote['distance_display'], 'WS')
        self.assertNotEqual(quote['final_amount'], 131)
        probed = [entry['params']['destination-pincode'] for entry in captured]
        self.assertIn('600001', probed)

    def test_kerala_pincode_keeps_the_within_state_api_result(self):
        quote, captured = self._quote('682001')
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]['params']['destination-pincode'], '682001')
        self.assertEqual(quote['distance_display'], 'WS')
        self.assertEqual(quote['base_tariff'], 111)
        self.assertEqual(quote['final_amount'], 131)

    def test_lakshadweep_other_states_quote_is_not_replaced(self):
        """Delhi to 682552 is already OS. Metro substitution is for WS only."""
        quote, captured = self._quote('682552', other_states=True)
        self.assertEqual(quote['distance_display'], 'OS')
        self.assertEqual(quote['final_amount'], 296)
        self.assertEqual(
            [entry['params']['destination-pincode'] for entry in captured],
            ['682552'],
        )

    def test_office_state_name_lakshadweep_uses_zone_metro(self):
        self.env['logistics.indiapost.office'].sudo().create({
            'pincode': '682560',
            'office_id': 'ld-test-682560',
            'office_name': 'Future Island SO',
            'state_name': 'Lakshadweep',
        })
        quote, captured = self._quote('682560')
        self.assertEqual(quote['destination_pincode'], '682560')
        self.assertEqual(quote['distance_display'], 'ZM')
        self.assertEqual(quote['final_amount'], 224)
        self.assertIn(
            '600001',
            [entry['params']['destination-pincode'] for entry in captured],
        )

    def test_lakshadweep_business_parcel_uses_zone_metro_slab(self):
        quote, captured = self._quote(
            '682552', article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL,
            weight_g=400,
        )
        self.assertEqual(quote['article_type'], ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(quote['destination_pincode'], '682552')
        self.assertEqual(quote['base_tariff'], 34)
        self.assertEqual(quote['final_amount'], 40)
        self.assertNotEqual(quote['final_amount'], 37)
        self.assertEqual(quote['distance_display'], 'ZM')
        bp_destinations = [
            entry['params']['destination-pincode']
            for entry in captured
            if 'business-parcel' in entry['path']
        ]
        self.assertEqual(bp_destinations, ['600001'])
        self.assertNotIn('682552', bp_destinations)
