"""India Post inbound webhooks: public POSTs that update shipment events.

The Customer Self-Service Portal is registered to
``/indiapost/bookingeventwebhook`` and ``/indiapost/othereventwebhook``.
These tests hit those paths with no session, never call India Post, and
never let a payload rewrite delivery charges.
"""

import json

from odoo.tests import HttpCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.tests.common import (
    CONFIG_PREFIX,
    IndiapostHermeticMixin,
)

BOOKING_PATH = ipc.BOOKING_WEBHOOK_PATH
OTHER_PATH = ipc.OTHER_WEBHOOK_PATH
ARTICLE_BOOKING = 'ETWHBK000016IN'
ARTICLE_TRACKING = 'ETWHOT000024IN'
ARTICLE_UNKNOWN = 'ETWHUNK00000IN'


@tagged('post_install', '-at_install')
class TestIndiapostWebhook(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls._ip_set_params((('indiapost_webhooks_enabled', 'True'),))

        cls.seller = cls.env['logistics.seller'].create({
            'name': 'India Post Webhook Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})

    def _new_shipment(self, article, **overrides):
        state = overrides.pop('state', None)
        vals = {
            'seller_id': self.seller.id,
            'shipping_to_name': 'Webhook Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Webhook article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        }
        vals.update(overrides)
        shipment = self.env['logistics.shipment'].create(vals)
        write_vals = {
            'indiapost_article_number': article,
        }
        if state:
            write_vals['state'] = state
        shipment.sudo().with_context(
            allow_shipment_state_write=True).write(write_vals)
        self.env.invalidate_all()
        return shipment

    def _post_json(self, path, payload=None, raw=None, content_type=None):
        headers = {'Content-Type': content_type or 'application/json'}
        if raw is not None:
            data = raw if isinstance(raw, bytes) else raw.encode()
            return self.url_open(path, data=data, headers=headers)
        if payload is None:
            return self.url_open(path, data=b'{}', headers=headers)
        return self.url_open(
            path, data=json.dumps(payload).encode(), headers=headers)

    def _assert_generic_ok(self, response, status=200):
        self.assertEqual(response.status_code, status)
        body = json.loads(response.text)
        self.assertEqual(body, {'status': 'ok'})
        self.assertNotIn('ETWH', response.text)
        self.assertNotIn('Webhook Customer', response.text)

    def _event_count(self, shipment):
        return self.env['logistics.shipment.event'].search_count([
            ('shipment_id', '=', shipment.id),
            ('indiapost_event_key', '!=', False),
        ])

    def test_empty_ping_on_both_routes_returns_ok(self):
        for path in (BOOKING_PATH, OTHER_PATH):
            with self.subTest(path=path, method='empty-json'):
                self._assert_generic_ok(self._post_json(path))
            with self.subTest(path=path, method='get'):
                self._assert_generic_ok(self.url_open(path))

    def test_booking_webhook_records_a_known_article(self):
        shipment = self._new_shipment(ARTICLE_BOOKING)
        charges_before = shipment.delivery_charges_total
        tariff_before = shipment.indiapost_calculated_tariff
        response = self._post_json(BOOKING_PATH, {
            'articleNumber': ARTICLE_BOOKING,
            'event': 'ITEM_BOOK',
            'eventId': 'book-1',
            'eventDateTime': '2026-04-28T10:15:00',
            'calculated_tariff': 9999,
            'delivery_charges_total': 1,
            'tariff': 50,
            'final_amount': 1,
        })
        self._assert_generic_ok(response)

        self.env.invalidate_all()
        shipment.invalidate_recordset()
        self.assertEqual(self._event_count(shipment), 1)
        self.assertEqual(shipment.state, 'in_transit')
        self.assertEqual(shipment.indiapost_booking_state, 'booked')
        self.assertEqual(shipment.delivery_charges_total, charges_before)
        self.assertEqual(shipment.indiapost_calculated_tariff, tariff_before)

        events = self.env['logistics.shipment.event'].search([
            ('shipment_id', '=', shipment.id),
            ('indiapost_event_key', '!=', False),
        ])
        self.assertEqual(events.event_type, 'indiapost_booked')

    def test_other_webhook_updates_tracking_like_the_poller(self):
        shipment = self._new_shipment(
            ARTICLE_TRACKING, state='in_transit')
        payload = {
            'data': {
                'article_number': ARTICLE_TRACKING,
                'event': 'Item Delivered',
                'event_id': 'del-1',
                'office': 'Kochi HO',
                'date': '2026-04-28',
                'time': '16:40:00',
            }
        }
        response = self._post_json(OTHER_PATH, payload)
        self._assert_generic_ok(response)

        self.env.invalidate_all()
        shipment.invalidate_recordset()
        self.assertEqual(shipment.state, 'delivered')
        self.assertEqual(self._event_count(shipment), 1)
        self.assertEqual(
            self.env['logistics.shipment.event'].search([
                ('shipment_id', '=', shipment.id),
                ('indiapost_event_key', '!=', False),
            ]).event_type,
            'delivered',
        )

        duplicate = self._post_json(OTHER_PATH, payload)
        self._assert_generic_ok(duplicate)
        self.env.invalidate_all()
        self.assertEqual(self._event_count(shipment), 1)
        self.assertEqual(shipment.state, 'delivered')

    def test_unknown_article_is_acknowledged_without_error(self):
        response = self._post_json(OTHER_PATH, {
            'barcode': ARTICLE_UNKNOWN,
            'event': 'ITEM_DELIVERY',
        })
        self._assert_generic_ok(response)
        self.assertFalse(self.env['logistics.shipment'].search([
            ('indiapost_article_number', '=', ARTICLE_UNKNOWN),
        ]))

    def test_unparseable_json_returns_400(self):
        response = self._post_json(
            BOOKING_PATH, raw=b'{not-json', content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.text), {'status': 'error'})

    def test_form_post_is_accepted(self):
        shipment = self._new_shipment('ETWHFORM0001IN', state='in_transit')
        response = self.url_open(OTHER_PATH, data={
            'article_number': 'ETWHFORM0001IN',
            'event': 'Item Dispatched',
        })
        self._assert_generic_ok(response)
        self.env.invalidate_all()
        shipment.invalidate_recordset()
        self.assertEqual(self._event_count(shipment), 1)
        self.assertEqual(shipment.state, 'in_transit')

    def test_disabled_webhooks_still_return_ok_and_do_not_apply(self):
        shipment = self._new_shipment('ETWHDOFF0001IN')
        self.env['ir.config_parameter'].sudo().set_param(
            CONFIG_PREFIX + 'indiapost_webhooks_enabled', 'False')
        try:
            response = self._post_json(BOOKING_PATH, {
                'articleNumber': 'ETWHDOFF0001IN',
                'event': 'ITEM_BOOK',
            })
            self._assert_generic_ok(response)
            self.env.invalidate_all()
            shipment.invalidate_recordset()
            self.assertEqual(self._event_count(shipment), 0)
            self.assertEqual(shipment.state, 'order_added')
        finally:
            self.env['ir.config_parameter'].sudo().set_param(
                CONFIG_PREFIX + 'indiapost_webhooks_enabled', 'True')
