"""Product-bound India Post barcode ranges.

India Post allotted EY to Speed Post and CX to Business Parcel. Production
allocation must consume only the matching range. Tests use fixture serials
that are not the live EY/CX blocks, and archive any production ranges they
did not create so a cloned database cannot burn a real AWB.
"""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

# Serials well away from the live production allotments (EY 54787840+,
# CX 05698773+) and from the TT test block (90000001+).
FIXTURE_EY_START = 10100001
FIXTURE_CX_START = 20100001
FIXTURE_SANDBOX_EY_START = 30100001


@tagged('post_install', '-at_install')
class TestIndiapostBarcodeAllocation(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.Range = cls.env['logistics.indiapost.barcode.range'].sudo()
        cls.Barcode = cls.env['logistics.indiapost.barcode'].sudo()
        # Never bump live production counters from this class.
        cls.Range.search([('environment', '=', 'production')]).write(
            {'active': False})
        cls.ey = cls._make_range(
            name='TEST production EY (Speed Post fixture)',
            environment='production',
            prefix='EY',
            start_serial=FIXTURE_EY_START,
            sequence=10,
        )
        cls.cx = cls._make_range(
            name='TEST production CX (Business Parcel fixture)',
            environment='production',
            prefix='CX',
            start_serial=FIXTURE_CX_START,
            sequence=20,
        )
        cls.sandbox_ey = cls._make_range(
            name='TEST sandbox EY (environment fixture)',
            environment='sandbox',
            prefix='EY',
            start_serial=FIXTURE_SANDBOX_EY_START,
            sequence=5,
        )
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Barcode Fixture Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Kochi Head Office',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})

    @classmethod
    def _make_range(cls, name, environment, prefix, start_serial, sequence):
        return cls.Range.create({
            'name': name,
            'environment': environment,
            'prefix': prefix,
            'start_serial': start_serial,
            'end_serial': start_serial + 19,
            'next_serial': start_serial,
            'sequence': sequence,
            'low_stock_threshold': 0,
        })

    def _new_shipment(self, **overrides):
        vals = {
            'seller_id': self.seller.id,
            'shipping_to_name': 'Barcode Customer',
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

    def _remaining(self, rng):
        rng.invalidate_recordset(['next_serial', 'remaining_count'])
        return rng.next_serial

    # ------------------------------------------------------------------
    # Prefix mapping persisted on the range
    # ------------------------------------------------------------------
    def test_ey_prefix_defaults_to_speed_post(self):
        self.assertEqual(self.ey.article_type, ipc.ARTICLE_TYPE_SPEED_POST)
        self.assertEqual(self.ey.prefix, 'EY')

    def test_cx_prefix_defaults_to_business_parcel(self):
        self.assertEqual(self.cx.article_type, ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(self.cx.prefix, 'CX')

    def test_uat_et_stays_untyped(self):
        rng = self.Range.create({
            'name': 'TEST UAT ET untyped',
            'environment': 'sandbox',
            'prefix': 'ET',
            'start_serial': 40100001,
            'end_serial': 40100010,
            'next_serial': 40100001,
            'sequence': 90,
        })
        self.assertFalse(rng.article_type)

    # ------------------------------------------------------------------
    # Production allocate is product-bound
    # ------------------------------------------------------------------
    def test_speed_post_allocate_uses_ey_not_cx(self):
        cx_before = self._remaining(self.cx)
        barcode = self.Range.allocate(
            environment='production',
            article_type=ipc.ARTICLE_TYPE_SPEED_POST,
        )
        self.assertEqual(barcode.range_id, self.ey)
        self.assertTrue(barcode.barcode.startswith('EY'))
        self.assertEqual(self._remaining(self.cx), cx_before)

    def test_business_parcel_allocate_uses_cx_not_ey(self):
        ey_before = self._remaining(self.ey)
        barcode = self.Range.allocate(
            environment='production',
            article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL,
        )
        self.assertEqual(barcode.range_id, self.cx)
        self.assertTrue(barcode.barcode.startswith('CX'))
        self.assertEqual(self._remaining(self.ey), ey_before)

    def test_shipment_article_type_selects_the_range(self):
        shipment = self._new_shipment(
            indiapost_article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        ey_before = self._remaining(self.ey)
        barcode = self.Range.allocate(
            shipment=shipment, environment='production')
        self.assertEqual(barcode.range_id, self.cx)
        self.assertEqual(self._remaining(self.ey), ey_before)

    def test_missing_cx_range_raises_for_business_parcel(self):
        self.cx.active = False
        with self.assertRaises(UserError) as error:
            self.Range.allocate(
                environment='production',
                article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL,
            )
        self.assertEqual(
            error.exception.args[0],
            'No production barcode range for Business Parcel (CX)',
        )
        self.assertFalse(
            self.Barcode.search([('range_id', '=', self.ey.id)]),
            'Speed Post stock must not be consumed when CX is missing.',
        )

    def test_missing_ey_range_raises_for_speed_post(self):
        self.ey.active = False
        with self.assertRaises(UserError) as error:
            self.Range.allocate(
                environment='production',
                article_type=ipc.ARTICLE_TYPE_SPEED_POST,
            )
        self.assertEqual(
            error.exception.args[0],
            'No production barcode range for Speed Post (EY)',
        )

    # ------------------------------------------------------------------
    # Environment is still sandbox vs production
    # ------------------------------------------------------------------
    def test_sandbox_allocate_does_not_consume_production(self):
        ey_before = self._remaining(self.ey)
        cx_before = self._remaining(self.cx)
        barcode = self.Range.allocate(
            environment='sandbox',
            article_type=ipc.ARTICLE_TYPE_SPEED_POST,
        )
        self.assertEqual(barcode.range_id.environment, 'sandbox')
        self.assertNotIn(barcode.range_id, (self.ey, self.cx))
        self.assertEqual(self._remaining(self.ey), ey_before)
        self.assertEqual(self._remaining(self.cx), cx_before)

    def test_production_allocate_does_not_consume_sandbox(self):
        sandbox_before = self._remaining(self.sandbox_ey)
        tt = self.Range.search([
            ('prefix', '=', 'TT'),
            ('environment', '=', 'sandbox'),
        ], limit=1)
        tt_before = self._remaining(tt) if tt else None
        barcode = self.Range.allocate(
            environment='production',
            article_type=ipc.ARTICLE_TYPE_SPEED_POST,
        )
        self.assertEqual(barcode.range_id, self.ey)
        self.assertEqual(self._remaining(self.sandbox_ey), sandbox_before)
        if tt:
            self.assertEqual(self._remaining(tt), tt_before)

    # ------------------------------------------------------------------
    # Already-issued barcodes stay put
    # ------------------------------------------------------------------
    def test_existing_ey_barcode_is_not_moved_for_business_parcel(self):
        shipment = self._new_shipment(
            indiapost_article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        existing = self.Barcode.create({
            'range_id': self.ey.id,
            'serial': self.ey.start_serial,
            'barcode': ipc.build_barcode('EY', self.ey.start_serial),
            'shipment_id': shipment.id,
            'state': 'booked',
        })
        ey_before = self._remaining(self.ey)
        cx_before = self._remaining(self.cx)
        barcode = self.Range.allocate(
            shipment=shipment,
            environment='production',
            article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL,
        )
        self.assertEqual(barcode, existing)
        self.assertEqual(barcode.barcode, existing.barcode)
        self.assertEqual(self._remaining(self.ey), ey_before)
        self.assertEqual(self._remaining(self.cx), cx_before)
