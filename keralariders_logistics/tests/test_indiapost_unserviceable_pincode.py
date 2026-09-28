"""India Post "not serviceable" destinations must read as a friendly warning.

695030 (Kanjirampara SO, Thiruvananthapuram) is a real PIN, but India Post's
API has no office for it: pincode-search returns zero records and both tariff
endpoints answer HTTP 422 ``Destination pincode 695030 is not serviceable —
no active delivery office configured``. Tariff and booking only send the
destination pincode, so there is no office id on our side to change; the
seller/staff must get the plain pincode warning instead of the raw 422 body.
"""

from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.models.indiapost_client import (
    IndiapostApiError,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

UNSERVICEABLE_PIN = '695030'
NOT_SERVICEABLE = (
    'Destination pincode %s is not serviceable \u2014 no active delivery '
    'office configured' % UNSERVICEABLE_PIN
)
FRIENDLY = '%s is not a valid delivery pincode' % UNSERVICEABLE_PIN

# Delivery offices India Post marks as not rolled out, alongside a
# non-delivery SO and a Branch Office. Only the delivery SO is bookable.
PIN_WITH_UNROLLED_OFFICES = '695099'
UNROLLED_OFFICES = [
    {
        'office_id': '22669901',
        'office_name': 'Kanjirampara Test SO',
        'office_type_code': 'SPO',
        'state_name': 'Kerala',
        'city_name': 'THIRUVANANTHAPURAM',
        'taluk_name': 'Thiruvananthapuram',
        'delivery_office_flag': True,
        'is_rolled_out': False,
    },
    {
        'office_id': '22669902',
        'office_name': 'Junction Test SO',
        'office_type_code': 'SPO',
        'state_name': 'Kerala',
        'city_name': 'THIRUVANANTHAPURAM',
        'delivery_office_flag': False,
        'is_rolled_out': True,
    },
    {
        'office_id': '22109903',
        'office_name': 'Test BO',
        'office_type_code': 'BPO',
        'state_name': 'Kerala',
        'delivery_office_flag': True,
        'is_rolled_out': True,
    },
]


def _not_serviceable_error():
    return IndiapostApiError(NOT_SERVICEABLE, status=422, payload={
        'success': False, 'error': NOT_SERVICEABLE,
    })


@tagged('post_install', '-at_install')
class TestIndiapostUnserviceablePincode(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.ClientModel = cls.registry['logistics.indiapost.client']
        cls.OfficeModel = cls.registry['logistics.indiapost.office']
        cls.Office = cls.env['logistics.indiapost.office']
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Unserviceable PIN Seller',
            'zip': '682001',
            'phone': '9847011111',
            'street': '12 Beach Road',
            'city': 'Kochi',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})
        district = cls.env['logistics.district'].search([
            ('name', 'ilike', 'Thiruvananthapuram'),
        ], limit=1)
        cls.shipment = cls.env['logistics.shipment'].create({
            'seller_id': cls.seller.id,
            'shipping_to_name': 'Kanjirampara Customer',
            'shipping_to_mobile': '9876543210',
            'shipping_to_address': '12 Test Road, Kanjirampara',
            'shipping_to_zip': UNSERVICEABLE_PIN,
            'shipping_to_district_id': district.id if district else False,
            'shipping_to_state_id': cls.env.ref('base.state_in_kl').id,
            'item_description': 'Books',
            'total_weight': 0.2,
            'length_cm': 21,
            'breadth_cm': 15,
            'height_cm': 5,
            **cls.env['logistics.shipment']._shipping_from_vals_for_seller(
                cls.seller),
        })

    def test_live_not_serviceable_wording_is_recognised(self):
        self.assertTrue(ipc.is_pincode_not_found_message(NOT_SERVICEABLE))
        self.assertEqual(
            ipc.extract_pincode_from_message(NOT_SERVICEABLE), UNSERVICEABLE_PIN)
        self.assertFalse(ipc.is_pincode_not_found_message(
            'Physical weight must be a whole number'))

    def test_tariff_quote_raises_friendly_user_error(self):
        with patch.object(
            self.ClientModel, 'call', side_effect=_not_serviceable_error(),
        ):
            with self.assertRaises(UserError) as err:
                self.env['logistics.indiapost.tariff'].quote(
                    '679576', UNSERVICEABLE_PIN, weight_g=200,
                    length_cm=21, breadth_cm=15, height_cm=5,
                    use_cache=False,
                )
        self.assertNotIsInstance(err.exception, IndiapostApiError)
        self.assertIn(FRIENDLY, str(err.exception))
        self.assertNotIn('HTTP 422', str(err.exception))

    def test_quote_safe_returns_blocking_friendly_error(self):
        with patch.object(
            self.ClientModel, 'call', side_effect=_not_serviceable_error(),
        ):
            result = self.env['logistics.indiapost.tariff'].quote_safe(
                '679576', UNSERVICEABLE_PIN, weight_g=200,
                length_cm=21, breadth_cm=15, height_cm=5, use_cache=False,
            )
        self.assertFalse(result['ok'])
        self.assertTrue(result['blocking'])
        self.assertIn(FRIENDLY, result['error'])

    def test_backend_get_quote_shows_friendly_message(self):
        with patch.object(
            self.ClientModel, 'call', side_effect=_not_serviceable_error(),
        ):
            with self.assertRaises(UserError) as err:
                self.shipment.action_indiapost_quote()
        self.assertIn(FRIENDLY, str(err.exception))
        self.assertNotIn('HTTP 422', str(err.exception))
        self.assertNotIn('no active delivery office', str(err.exception))

    def test_booking_rejection_records_friendly_message(self):
        with self._ip_patch_call(side_effect=_not_serviceable_error()):
            try:
                self.shipment.action_indiapost_book()
            except UserError:
                pass
        self.assertEqual(self.shipment.indiapost_booking_state, 'error')
        self.assertIn(FRIENDLY, self.shipment.indiapost_booking_error or '')
        self.assertNotIn('HTTP 422', self.shipment.indiapost_booking_error or '')

    def test_unrolled_delivery_office_is_still_selected(self):
        self.Office.search([('pincode', '=', PIN_WITH_UNROLLED_OFFICES)]).unlink()
        with patch.object(
            self.OfficeModel, '_ip_fetch_offices', return_value=UNROLLED_OFFICES,
        ):
            office = self.Office.resolve_booking_office(PIN_WITH_UNROLLED_OFFICES)
        self.assertEqual(office.office_id, '22669901')
        self.assertTrue(office.is_preferred)
        self.assertFalse(office.is_rolled_out)

    def test_empty_office_list_errors_clearly(self):
        self.Office.search([('pincode', '=', UNSERVICEABLE_PIN)]).unlink()
        with patch.object(self.OfficeModel, '_ip_fetch_offices', return_value=[]):
            with self.assertRaises(UserError) as err:
                self.Office.resolve_booking_office(UNSERVICEABLE_PIN)
        self.assertIn(UNSERVICEABLE_PIN, str(err.exception))
        self.assertIn('no bookable post office', str(err.exception))
        self.assertTrue(ipc.is_pincode_not_found_message(str(err.exception)))
