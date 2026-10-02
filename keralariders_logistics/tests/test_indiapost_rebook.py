"""Book a cancelled India Post shipment again on a new ARN.

The HTTP client is mocked the same way as the autobook tests: nothing here
calls India Post or consumes a live article number.
"""

from odoo import fields as odoo_fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.models.indiapost_client import (
    IndiapostApiError,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

# Inside the hermetic TT block, away from the pre-scan cancel fixtures.
_SERIAL_BASE = 90008001


@tagged('post_install', '-at_install')
class TestIndiapostRebook(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Rebook Seller',
            'email': 'rebook.seller@example.com',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Kochi Head Office',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.own_seller = cls.env['logistics.seller'].create({
            'name': 'Rebook Hub Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Kochi Head Office',
        })
        cls.own_seller.write({'fulfilment_method': 'own_network'})
        cls.Barcode = cls.env['logistics.indiapost.barcode'].sudo()
        cls.Range = cls.env['logistics.indiapost.barcode.range'].sudo()
        cls.tt_range = cls._ip_ensure_test_barcode_range()
        cls._serial_cursor = _SERIAL_BASE
        cls.portal_user = cls.env['res.users'].create({
            'name': 'Rebook Portal',
            'login': 'kx_rebook_portal',
            'password': 'kx_rebook_portal',
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

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
        while self.Barcode.search_count([
            ('barcode', '=', ipc.build_barcode('TT', serial)),
        ]):
            serial += 1
            type(self)._serial_cursor = serial + 1
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

    def _new_shipment(self, seller=None, **overrides):
        seller = seller or self.seller
        vals = {
            'order_id': self.order.id,
            'seller_id': seller.id,
            'shipping_to_name': 'Rebook Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Rebook article',
            'total_weight': 0.25,
            'length_cm': 29.0,
            'breadth_cm': 23.0,
            'height_cm': 2.0,
        }
        vals.update(overrides)
        if seller != self.seller:
            order = self.env['logistics.order'].create({'seller_id': seller.id})
            vals['order_id'] = order.id
        shipment = self.env['logistics.shipment'].create(vals)
        if shipment.fulfilment_method == 'indiapost':
            self._store_quote(shipment)
        return shipment

    def _booked_then_cancelled(self, shipment):
        """Pickup debit, a booked ARN, then the real pre-scan cancel."""
        serial, article = self._next_article()
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
        self.env['logistics.shipment.event'].create({
            'shipment_id': shipment.id,
            'event_type': 'indiapost_booked',
            'note': 'India Post article %s booked (batch test).' % article,
        })
        shipment.action_add_wallet_transaction()
        self.wallet.invalidate_recordset(['balance'])
        shipment.action_indiapost_cancel_pre_scan()
        self.wallet.invalidate_recordset(['balance'])
        return barcode

    def _shipping_debits(self, shipment):
        return self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('amount', '<', 0),
            ('reference', '!=', shipment._ip_scan_adj_reference()),
        ])

    def _cancel_credits(self, shipment):
        return self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', shipment._ip_cancel_reference()),
        ])

    def _booking_articles(self, mocked):
        articles = []
        for call in mocked.call_args_list:
            if call.kwargs.get('operation') != 'booking':
                continue
            body = call.kwargs.get('body') or {}
            if isinstance(body, dict):
                articles.extend(body.get('articles') or [])
        return articles

    def test_button_is_admin_only_on_cancelled_indiapost(self):
        view = self.env.ref('keralariders_logistics.view_shipment_form')
        arch = view.arch
        self.assertIn('name="action_indiapost_rebook"', arch)
        self.assertIn('Book again', arch)
        self.assertIn("state != 'cancelled'", arch)
        self.assertIn('group_logistics_admin', arch)
        self.assertIn('confirm=', arch)

        shipment = self._new_shipment()
        shipment.sudo().with_context(allow_shipment_state_write=True).write({
            'state': 'pickup_requested',
        })
        with self._ip_patch_call() as mocked:
            with self.assertRaises(UserError):
                shipment.action_indiapost_rebook()
        mocked.assert_not_called()
        self.assertEqual(shipment.state, 'pickup_requested')

        own = self._new_shipment(seller=self.own_seller)
        self.assertEqual(own.fulfilment_method, 'own_network')
        own.sudo().with_context(allow_shipment_state_write=True).write({
            'state': 'cancelled',
        })
        with self._ip_patch_call() as mocked:
            with self.assertRaises(UserError):
                own.action_indiapost_rebook()
        mocked.assert_not_called()
        self.assertEqual(own.state, 'cancelled')

        cancelled = self._new_shipment()
        void_barcode = self._booked_then_cancelled(cancelled)
        self.assertEqual(cancelled.state, 'cancelled')
        self.assertFalse(cancelled.portal_awb_printable())
        with self.assertRaises(AccessError):
            cancelled.with_user(self.portal_user).action_indiapost_rebook()
        self.assertEqual(void_barcode.state, 'void')
        self.assertEqual(cancelled.state, 'cancelled')
        self.assertFalse(cancelled.indiapost_article_number)

    def test_rebook_allocates_new_arn_and_debits_refund_once(self):
        shipment = self._new_shipment()
        void_barcode = self._booked_then_cancelled(shipment)
        void_arn = void_barcode.barcode
        charge = abs(self._cancel_credits(shipment).amount)
        balance_after_cancel = self.wallet.balance
        shipment.sudo().write({'indiapost_scan_adjusted': True})
        self.assertFalse(shipment.portal_awb_printable())

        with self._ip_patch_call() as mocked:
            shipment.action_indiapost_rebook()

        articles = self._booking_articles(mocked)
        self.assertEqual(len(articles), 1)
        self.assertNotEqual(articles[0]['barcode_no'], void_arn)
        self.assertEqual(articles[0]['barcode_no'], shipment.indiapost_article_number)
        self.assertTrue(articles[0]['sender_name'].startswith('[KeralaXpress] '))
        self.assertFalse((shipment.seller_id.name or '').startswith('[KeralaXpress]'))
        self.assertEqual(articles[0]['article_type'], ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(articles[0]['physical_weight'], 250)

        void_barcode.invalidate_recordset()
        self.assertEqual(void_barcode.state, 'void')
        self.assertNotEqual(shipment.indiapost_barcode_id, void_barcode)
        self.assertEqual(shipment.indiapost_barcode_id.state, 'booked')
        self.assertEqual(shipment.indiapost_booking_state, 'booked')
        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertEqual(shipment.order_id.state, 'pickup_requested')
        self.assertTrue(shipment.portal_awb_printable())
        self.assertTrue(shipment.indiapost_scan_adjusted)

        debits = self._shipping_debits(shipment)
        self.assertEqual(len(debits), 2)
        self.assertEqual(len(self._cancel_credits(shipment)), 1)
        self.assertAlmostEqual(shipment.wallet_transaction_id.amount, -charge,
                               places=2)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(
            self.wallet.balance, balance_after_cancel - charge, places=2)

    def test_rebook_does_not_debit_when_original_debit_remains(self):
        shipment = self._new_shipment()
        shipment.action_add_wallet_transaction()
        original = shipment.wallet_transaction_id
        serial, article = self._next_article()
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
            'state': 'cancelled',
        })
        barcode._ip_void_on_cancel(
            note='Voided by pre-scan cancel of %s' % shipment.name)
        shipment.sudo().write({
            'indiapost_article_number': False,
            'indiapost_barcode_id': False,
        })
        self.assertFalse(self._cancel_credits(shipment))
        self.wallet.invalidate_recordset(['balance'])
        balance = self.wallet.balance

        with self._ip_patch_call() as mocked:
            shipment.action_indiapost_rebook()

        articles = self._booking_articles(mocked)
        self.assertEqual(len(articles), 1)
        self.assertNotEqual(articles[0]['barcode_no'], article)
        self.assertEqual(len(self._shipping_debits(shipment)), 1)
        self.assertEqual(shipment.wallet_transaction_id, original)
        self.assertFalse(self._cancel_credits(shipment))
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, balance, places=2)
        barcode.invalidate_recordset()
        self.assertEqual(barcode.state, 'void')
        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertEqual(shipment.indiapost_booking_state, 'booked')

    def test_second_call_after_success_does_not_book_again(self):
        shipment = self._new_shipment()
        void_barcode = self._booked_then_cancelled(shipment)
        with self._ip_patch_call():
            shipment.action_indiapost_rebook()
        arn = shipment.indiapost_article_number
        barcode = shipment.indiapost_barcode_id
        debits = len(self._shipping_debits(shipment))
        self.wallet.invalidate_recordset(['balance'])
        balance = self.wallet.balance

        with self._ip_patch_call() as mocked:
            with self.assertRaises(UserError):
                shipment.action_indiapost_rebook()
        mocked.assert_not_called()
        self.assertEqual(shipment.indiapost_article_number, arn)
        self.assertEqual(shipment.indiapost_barcode_id, barcode)
        self.assertEqual(len(self._shipping_debits(shipment)), debits)
        self.assertEqual(len(self._cancel_credits(shipment)), 1)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, balance, places=2)
        self.assertEqual(shipment.state, 'pickup_requested')
        void_barcode.invalidate_recordset()
        self.assertEqual(void_barcode.state, 'void')

    def test_india_post_rejection_voids_new_arn_and_reverses_debit(self):
        shipment = self._new_shipment()
        void_barcode = self._booked_then_cancelled(shipment)
        shipment.sudo().write({'indiapost_scan_adjusted': True})
        self.wallet.invalidate_recordset(['balance'])
        balance_after_cancel = self.wallet.balance
        original_debit = self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('amount', '<', 0),
        ], order='id', limit=1)
        max_id = self.Barcode.search([], order='id desc', limit=1).id or 0

        with self._ip_patch_call(
            side_effect=IndiapostApiError('India Post rejected the article'),
        ) as mocked:
            shipment.action_indiapost_rebook()

        articles = self._booking_articles(mocked)
        self.assertEqual(len(articles), 1)
        self.assertNotEqual(articles[0]['barcode_no'], void_barcode.barcode)

        created = self.Barcode.search([('id', '>', max_id)])
        self.assertEqual(len(created), 1)
        self.assertEqual(created.barcode, articles[0]['barcode_no'])
        self.assertEqual(created.state, 'void')
        self.assertNotEqual(created.state, 'available')
        self.assertFalse(created.shipment_id)
        self.assertNotEqual(created, void_barcode)

        void_barcode.invalidate_recordset()
        self.assertEqual(void_barcode.state, 'void')
        self.assertEqual(shipment.state, 'cancelled')
        self.assertEqual(shipment.order_id.state, 'cancelled')
        self.assertFalse(shipment.indiapost_article_number)
        self.assertFalse(shipment.indiapost_barcode_id)
        self.assertFalse(shipment.portal_awb_printable())
        self.assertTrue(shipment.indiapost_scan_adjusted)
        self.assertEqual(shipment.wallet_transaction_id, original_debit)
        self.assertEqual(len(self._shipping_debits(shipment)), 1)
        self.assertEqual(len(self._cancel_credits(shipment)), 1)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(
            self.wallet.balance, balance_after_cancel, places=2)

        other = self._new_shipment()
        allocated = self.Range.allocate(shipment=other, environment='sandbox')
        self.assertNotIn(allocated, created | void_barcode)
        self.assertNotIn(allocated.barcode, {
            created.barcode, void_barcode.barcode,
        })
        self.assertEqual(allocated.state, 'reserved')
        self.assertEqual(created.state, 'void')
        self.assertEqual(void_barcode.state, 'void')
