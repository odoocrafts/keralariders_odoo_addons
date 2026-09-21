"""Pre-scan India Post cancel: wallet credit, ARN pool reuse, no live API."""

from odoo import fields as odoo_fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

# Serials inside the hermetic TT test block, away from live allotments.
_SERIAL_BASE = 90001001


@tagged('post_install', '-at_install')
class TestIndiapostPreScanCancel(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Pre-scan Cancel Seller',
            'email': 'prescan.cancel@example.com',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.Range = cls.env['logistics.indiapost.barcode.range'].sudo()
        cls.Barcode = cls.env['logistics.indiapost.barcode'].sudo()
        cls.Mail = cls.env['mail.mail'].sudo()
        cls.tt_range = cls._ip_ensure_test_barcode_range()
        cls._serial_cursor = _SERIAL_BASE

    def setUp(self):
        super().setUp()
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        self.wallet.invalidate_recordset(['balance'])
        self.order = self.env['logistics.order'].create({
            'seller_id': self.seller.id,
        })

    def _next_article(self):
        serial = type(self)._serial_cursor
        type(self)._serial_cursor += 1
        return serial, ipc.build_barcode('TT', serial)

    def _store_quote(self, shipment, total=118.0):
        shipment.write({
            'indiapost_base_tariff': 100.0,
            'indiapost_vas_charges': 0.0,
            'indiapost_tax_amount': 18.0,
            'indiapost_total_tariff': total,
            'indiapost_quoted_weight_g': ipc.band_weight(
                ipc.kg_to_grams(shipment.total_weight)),
            'indiapost_tariff_quoted_on': odoo_fields.Datetime.now(),
            'indiapost_quote_signature': shipment._ip_quote_signature(),
        })

    def _new_ip_shipment(self, **overrides):
        serial, article = self._next_article()
        vals = {
            'order_id': self.order.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'Cancel Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Cancel article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        }
        vals.update(overrides)
        shipment = self.env['logistics.shipment'].create(vals)
        self._store_quote(shipment)
        barcode = self.Barcode.create({
            'range_id': self.tt_range.id,
            'serial': serial,
            'barcode': article,
            'shipment_id': shipment.id,
            'state': 'booked',
        })
        shipment.sudo().with_context(allow_shipment_state_write=True).write({
            'indiapost_article_number': article,
            'indiapost_barcode_id': barcode.id,
            'indiapost_booking_state': 'booked',
            'state': 'pickup_requested',
        })
        # Local process-articles confirmation (no tracking key) must not block.
        self.env['logistics.shipment.event'].create({
            'shipment_id': shipment.id,
            'event_type': 'indiapost_booked',
            'note': 'India Post article %s booked (batch test).' % article,
        })
        shipment.action_add_wallet_transaction()
        self.wallet.invalidate_recordset(['balance'])
        return shipment

    def _cancel_lines(self, shipment):
        return self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', shipment._ip_cancel_reference()),
        ])

    def _outgoing_cancel_mails(self, shipment):
        return self.Mail.search([
            ('model', '=', 'logistics.shipment'),
            ('res_id', '=', shipment.id),
            ('subject', 'ilike', 'booking cancelled'),
        ])

    def test_pre_scan_cancel_credits_releases_arn_and_mails(self):
        shipment = self._new_ip_shipment()
        debit = abs(shipment.wallet_transaction_id.amount)
        arn = shipment.indiapost_article_number
        barcode = shipment.indiapost_barcode_id
        balance_after_debit = self.wallet.balance
        awb = shipment.name

        shipment.action_indiapost_cancel_pre_scan()

        self.assertEqual(shipment.state, 'cancelled')
        self.assertFalse(shipment.indiapost_article_number)
        self.assertFalse(shipment.indiapost_barcode_id)
        barcode.invalidate_recordset()
        self.assertEqual(barcode.state, 'available')
        self.assertFalse(barcode.shipment_id)

        credits = self._cancel_lines(shipment)
        self.assertEqual(len(credits), 1)
        self.assertAlmostEqual(credits.amount, debit, places=2)
        self.assertEqual(credits.reference, 'IP-CANCEL:%s' % shipment.id)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(
            self.wallet.balance, balance_after_debit + debit, places=2)

        mails = self._outgoing_cancel_mails(shipment)
        self.assertEqual(len(mails), 1)
        body = mails.body_html or ''
        self.assertIn(awb, body)
        self.assertIn('returned', body.lower())

        # Released ARN is preferred over minting a new serial on the same range.
        others = self.Range.search([
            ('id', '!=', self.tt_range.id),
            ('environment', '=', 'sandbox'),
            ('active', '=', True),
        ])
        others.write({'active': False})
        try:
            next_ship = self.env['logistics.shipment'].create({
                'order_id': self.order.id,
                'seller_id': self.seller.id,
                'shipping_to_name': 'Reuse Customer',
                'shipping_to_address': '99 Reuse Road',
                'shipping_to_zip': '695001',
                'shipping_to_mobile': '9876543211',
                'item_description': 'Reuse article',
                'total_weight': 1.0,
                'length_cm': 30,
                'breadth_cm': 20,
                'height_cm': 15,
            })
            reused = self.Range.allocate(
                shipment=next_ship, environment='sandbox')
            self.assertEqual(reused, barcode)
            self.assertEqual(reused.barcode, arn)
            self.assertEqual(reused.state, 'reserved')
            self.assertEqual(reused.shipment_id, next_ship)
        finally:
            others.write({'active': True})

    def test_retry_is_idempotent(self):
        shipment = self._new_ip_shipment()
        barcode = shipment.indiapost_barcode_id
        article = barcode.barcode
        shipment.action_indiapost_cancel_pre_scan()
        balance = self.wallet.balance
        shipment.action_indiapost_cancel_pre_scan()
        self.assertEqual(len(self._cancel_lines(shipment)), 1)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, balance, places=2)
        self.assertEqual(
            self.Barcode.search_count([('barcode', '=', article)]), 1)
        self.assertEqual(len(self._outgoing_cancel_mails(shipment)), 1)

    def test_after_pickup_scan_event_refused(self):
        shipment = self._new_ip_shipment()
        balance = self.wallet.balance
        barcode = shipment.indiapost_barcode_id
        article = shipment.indiapost_article_number
        self.env['logistics.shipment.event'].create({
            'shipment_id': shipment.id,
            'event_type': 'pickup_scan',
            'note': 'Item Pickedup',
            'indiapost_event_code': 'PICKEDUP',
            'indiapost_event_key': 'scan-picked-%s' % shipment.id,
        })
        with self.assertRaises(UserError):
            shipment.action_indiapost_cancel_pre_scan()
        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertEqual(shipment.indiapost_article_number, article)
        self.assertEqual(barcode.state, 'booked')
        self.assertFalse(self._cancel_lines(shipment))
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, balance, places=2)
        self.assertFalse(self._outgoing_cancel_mails(shipment))

    def test_after_state_picked_refused(self):
        shipment = self._new_ip_shipment()
        balance = self.wallet.balance
        shipment.sudo().with_context(allow_shipment_state_write=True).write({
            'state': 'picked',
        })
        with self.assertRaises(UserError):
            shipment.action_indiapost_cancel_pre_scan()
        self.assertFalse(self._cancel_lines(shipment))
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, balance, places=2)

    def test_tracking_item_booked_blocks_cancel(self):
        shipment = self._new_ip_shipment()
        self.env['logistics.shipment.event'].create({
            'shipment_id': shipment.id,
            'event_type': 'indiapost_booked',
            'note': 'Item Booked',
            'indiapost_event_code': 'ITEM BOOKED',
            'indiapost_event_key': 'track-booked-%s' % shipment.id,
        })
        self.assertFalse(shipment.portal_indiapost_cancel_allowed())
        with self.assertRaises(UserError):
            shipment.action_indiapost_cancel_pre_scan()

    def test_scan_adj_wallet_line_blocks_cancel(self):
        shipment = self._new_ip_shipment()
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': -5.0,
            'shipment_id': shipment.id,
            'reference': shipment._ip_scan_adj_reference(),
        })
        with self.assertRaises(UserError):
            shipment.action_indiapost_cancel_pre_scan()
        self.assertFalse(self._cancel_lines(shipment))

    def test_own_network_cancel_unchanged(self):
        own_seller = self.env['logistics.seller'].create({
            'name': 'Own Network Cancel Seller',
            'zip': '682001',
        })
        own_seller.write({'fulfilment_method': 'own_network'})
        wallet = own_seller.wallet_ids[0]
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        order = self.env['logistics.order'].create({'seller_id': own_seller.id})
        shipment = self.env['logistics.shipment'].create({
            'order_id': order.id,
            'seller_id': own_seller.id,
            'shipping_to_name': 'Own Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Own parcel',
            'total_weight': 1.0,
        })
        shipment.sudo().with_context(allow_shipment_state_write=True).write({
            'state': 'pickup_requested',
        })
        shipment.action_add_wallet_transaction()
        debit = shipment.wallet_transaction_id
        balance = wallet.balance
        self.assertFalse(shipment.portal_indiapost_cancel_allowed())
        shipment.action_cancel_shipment()
        self.assertEqual(shipment.state, 'cancelled')
        self.assertTrue(debit.exists())
        wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(wallet.balance, balance, places=2)
        self.assertFalse(self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=like', 'IP-CANCEL:%'),
        ]))
