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
ANDAMAN_PIN = '744302'

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

ANDAMAN_OFFICES = [{
    'office_id': '74430201',
    'office_name': 'Bambooflat SO',
    'office_type_code': 'SPO',
    'state_name': 'Andaman and Nicobar Islands',
    'city_name': 'SOUTH ANDAMAN',
    'taluk_name': 'Ferrargunj',
    'village_name': 'Bambooflat',
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
            self.assertIn('not a valid delivery pincode', str(err.exception))
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
        self.assertIn('not a valid delivery pincode', str(err.exception))
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

    def _shipment_vals(self, seller, dest_zip, **overrides):
        vals = {
            'seller_id': seller.id,
            'shipping_to_name': 'National Customer',
            'shipping_to_address': '12 Kodambakkam High Road',
            'shipping_to_zip': dest_zip,
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        }
        vals.update(overrides)
        return vals

    def test_ip_seller_creates_shipment_to_national_pin_without_hub(self):
        """India Post + Chennai PIN: order/shipment save; no fake dest hub."""
        self._clear_chennai_cache()
        with patch.object(
            self.OfficeModel, '_ip_fetch_offices', return_value=CHENNAI_OFFICES,
        ):
            self.District.resolve_destination_from_pincode(
                CHENNAI_PIN, allow_indiapost=True, raise_if_missing=True,
            )
            shipment = self.env['logistics.shipment'].create(
                self._shipment_vals(self.ip_seller, CHENNAI_PIN),
            )
        self.assertEqual(shipment.fulfilment_method, 'indiapost')
        self.assertEqual(shipment.shipping_to_zip, CHENNAI_PIN)
        self.assertTrue(shipment.source_hub_id, 'seller-side Kerala hub expected')
        self.assertFalse(
            shipment.destination_hub_id,
            'must not invent a hub for a non-Kerala India Post destination',
        )
        with self.assertRaises(UserError) as err:
            self.env['logistics.hub'].get_hub_from_pincode(CHENNAI_PIN)
        self.assertIn('Cannot find any Hub assigned to pincode', str(err.exception))

    def test_own_network_seller_still_requires_dest_hub(self):
        """Own-network create still raises when the destination has no hub."""
        # A Kerala-table PIN whose district has no hub (not a national IP pin).
        orphan_district = self.District.create({
            'name': 'Hubless Test District',
            'state_id': self.env.ref('base.state_in_kl').id,
        })
        orphan_pin = '699991'
        self.assertFalse(self.env['logistics.hub'].search([
            ('district_id', '=', orphan_district.id),
        ]))
        self.Pincode.create({
            'name': orphan_pin,
            'district_name': 'Hubless Test District',
            'state_name': 'Kerala',
        })
        with self.assertRaises(UserError) as err:
            self.env['logistics.shipment'].create(
                self._shipment_vals(self.hub_seller, orphan_pin),
            )
        self.assertIn('Cannot find any Hub assigned to pincode', str(err.exception))
        self.assertIn(orphan_pin, str(err.exception))

    def test_andaman_islands_state_alias_resolves(self):
        """India Post 'Andaman and Nicobar Islands' → Odoo Andaman and Nicobar."""
        self.Office.search([('pincode', '=', ANDAMAN_PIN)]).unlink()
        an_state = self.env['res.country.state'].search([
            ('country_id.code', '=', 'IN'),
            ('name', '=ilike', 'Andaman and Nicobar'),
        ], limit=1)
        self.assertTrue(
            an_state,
            'Odoo India must include state Andaman and Nicobar',
        )
        self.District.search([
            ('name', 'ilike', 'South Andaman'),
            ('state_id', '=', an_state.id),
        ]).unlink()
        with patch.object(
            self.OfficeModel, '_ip_fetch_offices', return_value=ANDAMAN_OFFICES,
        ) as fetch:
            resolved = self.District.resolve_destination_from_pincode(
                ANDAMAN_PIN, allow_indiapost=True, raise_if_missing=True,
            )
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(resolved['source'], 'indiapost')
        self.assertEqual(resolved['state_id'], an_state)
        self.assertEqual(
            (resolved['state_id'].name or '').lower(),
            'andaman and nicobar',
        )
        self.assertFalse(self.Pincode.search([('name', '=', ANDAMAN_PIN)]))

    def test_unknown_indiapost_state_still_errors(self):
        offices = [{
            **CHENNAI_OFFICES[0],
            'state_name': 'Atlantis Federated Islands',
            'city_name': 'POSEIDON',
        }]
        self.Office.search([('pincode', '=', CHENNAI_PIN)]).unlink()
        with patch.object(
            self.OfficeModel, '_ip_fetch_offices', return_value=offices,
        ):
            with self.assertRaises(UserError) as err:
                self.District.resolve_destination_from_pincode(
                    CHENNAI_PIN, allow_indiapost=True, raise_if_missing=True,
                )
        self.assertIn('unrecognized state', str(err.exception).lower())
        self.assertIn('Atlantis Federated Islands', str(err.exception))

    def test_kerala_exact_state_match_without_alias(self):
        """Plain Kerala label still resolves via exact match (no alias needed)."""
        state = self.District._find_indian_state('Kerala')
        self.assertTrue(state)
        self.assertEqual(state.code, 'KL')
        self.assertEqual(state.name, 'Kerala')
        # Case-insensitive exact still works for India Post ALL-CAPS.
        state_caps = self.District._find_indian_state('KERALA')
        self.assertEqual(state_caps, state)
