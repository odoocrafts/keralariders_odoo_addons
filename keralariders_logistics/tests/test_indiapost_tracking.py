"""India Post scan clocks are IST; Odoo stores naive UTC.

Hermetic: no India Post HTTP. Covers parse, storage, and public display.
"""

import datetime

from odoo import fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

IST_OFFSET = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


@tagged('post_install', '-at_install')
class TestIndiapostTrackingTimezone(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'India Post Clock Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})

    def _new_shipment(self, article='EY547878449IN', **overrides):
        vals = {
            'seller_id': self.seller.id,
            'shipping_to_name': 'Clock Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Clock article',
            'total_weight': 0.4,
            'length_cm': 20,
            'breadth_cm': 15,
            'height_cm': 10,
        }
        vals.update(overrides)
        shipment = self.env['logistics.shipment'].create(vals)
        shipment.sudo().with_context(allow_shipment_state_write=True).write({
            'indiapost_article_number': article,
            'state': 'in_transit',
        })
        return shipment

    def test_helper_naive_ist_becomes_odoo_utc(self):
        naive = datetime.datetime(2026, 9, 16, 10, 6)
        self.assertEqual(
            ipc.to_odoo_utc(naive),
            datetime.datetime(2026, 9, 16, 4, 36),
        )

    def test_helper_aware_utc_is_not_shifted_again(self):
        aware = datetime.datetime(2026, 9, 16, 4, 36, tzinfo=datetime.timezone.utc)
        self.assertEqual(
            ipc.to_odoo_utc(aware),
            datetime.datetime(2026, 9, 16, 4, 36),
        )
        aware_ist = datetime.datetime(2026, 9, 16, 10, 6, tzinfo=IST_OFFSET)
        self.assertEqual(
            ipc.to_odoo_utc(aware_ist),
            datetime.datetime(2026, 9, 16, 4, 36),
        )

    def test_parse_event_date_time_dd_mm_yyyy_ist(self):
        Tracking = self.env['logistics.indiapost.tracking']
        moment = Tracking._ip_scan_datetime({
            'eventDate': '16-09-2026',
            'eventTime': '10:06',
        })
        self.assertEqual(moment, datetime.datetime(2026, 9, 16, 4, 36))

    def test_parse_bulk_date_iso_z_with_ist_clock(self):
        Tracking = self.env['logistics.indiapost.tracking']
        moment = Tracking._ip_scan_datetime({
            'date': '2026-09-16T00:00:00Z',
            'time': '10:06:00',
        })
        self.assertEqual(moment, datetime.datetime(2026, 9, 16, 4, 36))

    def test_parse_naive_iso_is_ist(self):
        Tracking = self.env['logistics.indiapost.tracking']
        moment = Tracking._ip_scan_datetime({
            'eventDateTime': '2026-09-16T10:06:00',
        })
        self.assertEqual(moment, datetime.datetime(2026, 9, 16, 4, 36))

    def test_parse_zulu_iso_stays_utc(self):
        Tracking = self.env['logistics.indiapost.tracking']
        moment = Tracking._ip_scan_datetime({
            'eventDateTime': '2026-09-16T04:36:00Z',
        })
        self.assertEqual(moment, datetime.datetime(2026, 9, 16, 4, 36))

    def test_apply_tracking_stores_utc_and_displays_ist(self):
        shipment = self._new_shipment()
        self.env['logistics.indiapost.tracking']._ip_apply_tracking(shipment, {
            'tracking_details': [{
                'event': 'Out for delivery',
                'date': '16-09-2026',
                'time': '10:06',
                'office': 'Pontianam BO',
                'officeid': '1',
            }],
        })
        event = self.env['logistics.shipment.event'].search([
            ('shipment_id', '=', shipment.id),
            ('indiapost_event_key', '!=', False),
        ])
        self.assertEqual(len(event), 1)
        self.assertEqual(
            fields.Datetime.to_string(event.event_time),
            '2026-09-16 04:36:00',
        )
        self.assertTrue(
            event.indiapost_event_key.startswith('20260916100600'),
            event.indiapost_event_key,
        )
        display = shipment.with_context(tz='Asia/Kolkata')._format_tracking_datetime(
            event.event_time)
        self.assertEqual(display, '16 Sep 2026, 10:06 AM')

    def test_same_scan_is_not_duplicated_after_timezone_fix(self):
        shipment = self._new_shipment(article='EY547000002IN')
        Event = self.env['logistics.shipment.event'].sudo()
        Event.create({
            'shipment_id': shipment.id,
            'event_type': 'out_for_delivery',
            'event_time': datetime.datetime(2026, 9, 16, 10, 6),
            'note': 'Out for delivery',
            'indiapost_event_code': 'Out for delivery',
            'indiapost_event_key': '20260916100600|1|Out for delivery',
            'indiapost_office_name': 'Pontianam BO',
        })
        self.env['logistics.indiapost.tracking']._ip_apply_tracking(shipment, {
            'tracking_details': [{
                'event': 'Out for delivery',
                'eventDate': '16-09-2026',
                'eventTime': '10:06',
                'office': 'Pontianam BO',
                'officeid': '1',
            }],
        })
        events = Event.search([
            ('shipment_id', '=', shipment.id),
            ('indiapost_event_key', '!=', False),
        ])
        self.assertEqual(len(events), 1)
        self.assertEqual(
            fields.Datetime.to_string(events.event_time),
            '2026-09-16 10:06:00',
        )
