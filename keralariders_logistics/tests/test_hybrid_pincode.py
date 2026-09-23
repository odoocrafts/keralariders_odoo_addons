"""Hybrid destination pincode lookup for India Post sellers.

Kerala ``logistics.pincode`` first; on a miss India Post ``pincode-search``
fills ``logistics.indiapost.office`` and a national ``logistics.district`` is
created on demand. Hub sellers stay Kerala-only. No live India Post HTTP.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models.indiapost_client import (
    IndiapostApiError,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


CHENNAI_PIN = '600044'
KERALA_PIN = '695001'

CHENNAI_OFFICES = [{
    'office_id': '22840111',
    'office_name': 'Kodambakkam SO',
    'office_type_code': 'SPO',
    'state_name': 'TAMIL NADU',
    'city_name': 'CHENNAI',
    'taluk_name': 'Chennai',
    'village_name': 'Kodambakkam',
    'delivery_office_flag': True,
    'is_rolled_out': True,
}]


@tagged('post_install', '-at_install')
class TestHybridPincodeLookup(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.District = cls.env['logistics.district']
        cls.Office = cls.env['logistics.indiapost.office']
        cls.Pincode = cls.env['logistics.pincode']
        cls.OfficeModel = cls.registry['logistics.indiapost.office']

        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Hybrid PIN IP Seller',
            'zip': '682001',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})

        cls.hub_seller = cls.env['logistics.seller'].create({
            'name': 'Hybrid PIN Hub Seller',
            'zip': '682001',
        })
        cls.hub_seller.write({'fulfilment_method': 'own_network'})

    def _clear_chennai_cache(self):
        self.Office.search([('pincode', '=', CHENNAI_PIN)]).unlink()
        self.District.search([
            ('name', 'ilike', 'Chennai'),
            ('state_id.code', '=', 'TN'),
        ]).unlink()

    def test_ip_seller_national_pin_syncs_caches_and_creates_district(self):
        self._clear_chennai_cache()
        with patch.object(
            self.OfficeModel, '_ip_fetch_offices', return_value=CHENNAI_OFFICES,
        ) as fetch:
            resolved = self.District.resolve_destination_from_pincode(
                CHENNAI_PIN, allow_indiapost=True, raise_if_missing=True,
            )
            self.assertEqual(fetch.call_count, 1)

        self.assertEqual(resolved['source'], 'indiapost')
        self.assertTrue(resolved['district_id'])
        self.assertEqual(resolved['district_id'].name, 'Chennai')
        self.assertEqual(resolved['state_id'].code, 'TN')
        cached = self.Office.search([
            ('pincode', '=', CHENNAI_PIN), ('is_preferred', '=', True),
        ])
        self.assertTrue(cached)
        self.assertEqual(cached.city_name, 'CHENNAI')
        # Must not fake hub serviceability.
        self.assertFalse(self.Pincode.search([('name', '=', CHENNAI_PIN)]))

    def test_second_resolve_uses_warm_office_cache(self):
        self._clear_chennai_cache()
        with patch.object(
            self.OfficeModel, '_ip_fetch_offices', return_value=CHENNAI_OFFICES,
        ) as fetch:
            self.District.resolve_destination_from_pincode(
                CHENNAI_PIN, allow_indiapost=True, raise_if_missing=True,
            )
            self.assertEqual(fetch.call_count, 1)
            self.District.resolve_destination_from_pincode(
                CHENNAI_PIN, allow_indiapost=True, raise_if_missing=True,
            )
            # Warm TTL cache: resolve_booking_office must not call the API again.
            self.assertEqual(fetch.call_count, 1)

    def test_hub_seller_still_rejects_national_pin(self):
        self._clear_chennai_cache()
        with patch.object(
            self.OfficeModel, '_ip_fetch_offices', return_value=CHENNAI_OFFICES,
        ) as fetch:
            with self.assertRaises(UserError) as err:
                self.District.resolve_destination_from_pincode(
                    CHENNAI_PIN, allow_indiapost=False, raise_if_missing=True,
                )
            self.assertIn('Unknown pincode', str(err.exception))
            self.assertEqual(fetch.call_count, 0)
        self.assertFalse(self.Office.search([('pincode', '=', CHENNAI_PIN)]))
        self.assertFalse(self.Pincode.search([('name', '=', CHENNAI_PIN)]))

    def test_ip_seller_kerala_pin_uses_local_table_without_http(self):
        with patch.object(
            self.OfficeModel, '_ip_fetch_offices', return_value=CHENNAI_OFFICES,
        ) as fetch:
            resolved = self.District.resolve_destination_from_pincode(
                KERALA_PIN, allow_indiapost=True, raise_if_missing=True,
            )
            self.assertEqual(fetch.call_count, 0)
        self.assertEqual(resolved['source'], 'local')
        self.assertTrue(resolved['district_id'])
        self.assertIn(
            'thiruvananthapuram',
            (resolved['district_id'].name or '').lower(),
        )

    def test_empty_indiapost_result_errors_without_inventing_district(self):
        self._clear_chennai_cache()
        before = self.District.search_count([])
        with patch.object(self.OfficeModel, '_ip_fetch_offices', return_value=[]):
            with self.assertRaises(UserError) as err:
                self.District.resolve_destination_from_pincode(
                    CHENNAI_PIN, allow_indiapost=True, raise_if_missing=True,
                )
        self.assertIn('Unknown pincode', str(err.exception))
        self.assertEqual(self.District.search_count([]), before)
        self.assertFalse(self.Pincode.search([('name', '=', CHENNAI_PIN)]))

    def test_api_down_without_cache_errors_clearly(self):
        self._clear_chennai_cache()
        with patch.object(
            self.OfficeModel,
            '_ip_fetch_offices',
            side_effect=IndiapostApiError('network down'),
        ):
            with self.assertRaises(UserError) as err:
                self.District.resolve_destination_from_pincode(
                    CHENNAI_PIN, allow_indiapost=True, raise_if_missing=True,
                )
        self.assertIn('could not verify', str(err.exception).lower())
        self.assertIn(CHENNAI_PIN, str(err.exception))
        self.assertFalse(
            self.District.search([
                ('name', 'ilike', 'Chennai'),
                ('state_id.code', '=', 'TN'),
            ])
        )

    def test_seller_fulfilment_flags_match_allow_indiapost(self):
        self.assertTrue(self.ip_seller._ip_uses_indiapost())
        self.assertFalse(self.hub_seller._ip_uses_indiapost())
