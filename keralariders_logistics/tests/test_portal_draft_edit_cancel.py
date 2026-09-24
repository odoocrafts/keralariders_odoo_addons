"""Draft (Order Added) Edit / Cancel on the seller portal.

Wallet is not debited until Request Pickup. Cancelling a draft must not mint
a wallet credit when there is no debit, must work without an ARN, and must
refuse edit after pickup is requested. Templates expose Edit and Cancel only
while state is order_added.
"""

import re

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


@tagged('post_install', '-at_install')
class TestDraftCancelModel(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Draft Cancel Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'own_network'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'Draft Cancel IP Seller',
            'zip': '682001',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.ip_wallet = cls.ip_seller.wallet_ids[0]

    def setUp(self):
        super().setUp()
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        self.wallet.invalidate_recordset(['balance'])
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.ip_wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        self.ip_wallet.invalidate_recordset(['balance'])

    def _draft_shipment(self, seller, **overrides):
        order = self.env['logistics.order'].create({'seller_id': seller.id})
        vals = {
            'order_id': order.id,
            'seller_id': seller.id,
            'shipping_to_name': 'Draft Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Draft item',
            'total_weight': 1.0,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
            'state': 'order_added',
        }
        vals.update(overrides)
        return self.env['logistics.shipment'].create(vals)

    def test_draft_cancel_no_debit_no_wallet_credit(self):
        shipment = self._draft_shipment(self.seller)
        balance = self.wallet.balance
        self.assertFalse(shipment.wallet_transaction_id)

        shipment.action_cancel_order_added()

        self.assertEqual(shipment.state, 'cancelled')
        self.assertEqual(shipment.order_id.state, 'cancelled')
        credits = self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', shipment._ip_cancel_reference()),
        ])
        self.assertFalse(credits)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, balance, places=2)

    def test_draft_cancel_no_arn(self):
        shipment = self._draft_shipment(self.ip_seller)
        self.assertFalse(shipment.indiapost_article_number)
        self.assertFalse(shipment.indiapost_barcode_id)

        shipment.action_cancel_order_added()

        self.assertEqual(shipment.state, 'cancelled')
        self.assertFalse(shipment.indiapost_article_number)
        self.assertFalse(shipment.indiapost_barcode_id)

    def test_draft_cancel_idempotent(self):
        shipment = self._draft_shipment(self.seller)
        shipment.action_cancel_order_added()
        shipment.action_cancel_order_added()
        self.assertEqual(shipment.state, 'cancelled')

    def test_draft_cancel_unexpected_debit_credits_once(self):
        shipment = self._draft_shipment(self.seller)
        shipment.action_add_wallet_transaction()
        self.assertTrue(shipment.wallet_transaction_id)
        debit = abs(shipment.wallet_transaction_id.amount)
        balance_after_debit = self.wallet.balance

        shipment.action_cancel_order_added()

        self.assertEqual(shipment.state, 'cancelled')
        credits = self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', shipment._ip_cancel_reference()),
        ])
        self.assertEqual(len(credits), 1)
        self.assertAlmostEqual(credits.amount, debit, places=2)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(
            self.wallet.balance, balance_after_debit + debit, places=2)
        # Retry must not credit again.
        shipment.action_cancel_order_added()
        self.assertEqual(len(credits.exists()), 1)

    def test_edit_rejected_once_pickup_requested(self):
        shipment = self._draft_shipment(self.seller)
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'pickup_requested',
        })
        self.assertFalse(shipment.portal_draft_edit_allowed())
        self.assertFalse(shipment.portal_draft_cancel_allowed())
        with self.assertRaises(UserError):
            shipment.action_cancel_order_added()


@tagged('post_install', '-at_install')
class TestPortalDraftEditCancel(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Portal Draft Edit Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'own_network'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.portal_login = 'kx_draft_edit_cancel'
        cls.env['res.users'].create({
            'name': 'Draft Edit Portal',
            'login': cls.portal_login,
            'password': cls.portal_login,
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

    def _csrf(self, html):
        match = re.search(
            r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html)
        self.assertTrue(match, 'no csrf_token in the rendered page')
        return match.group(1)

    def _create_draft(self):
        order = self.env['logistics.order'].create({
            'seller_id': self.seller.id,
        })
        shipment = self.env['logistics.shipment'].create({
            'order_id': order.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'Portal Draft Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Toys',
            'total_weight': 1.5,
            'state': 'order_added',
        })
        return order, shipment

    def test_shipments_list_shows_edit_and_cancel_for_order_added(self):
        _order, shipment = self._create_draft()
        self.authenticate(self.portal_login, self.portal_login)
        page = self.url_open('/my/shipments')
        self.assertEqual(page.status_code, 200)
        self.assertIn('/my/shipments/%s/edit' % shipment.id, page.text)
        self.assertIn('/my/shipments/%s/cancel' % shipment.id, page.text)
        self.assertRegex(
            page.text,
            r'Cancel this draft shipment\? Nothing has been charged',
        )
        # Request Pickup visible for drafts (not clipped behind cell ellipsis)
        # and requires a wallet-debit confirm before submit.
        self.assertIn('Request Pickup', page.text)
        self.assertIn('action="/my/shipments/request_pickup"', page.text)
        self.assertRegex(
            page.text,
            r"Request pickup for AWB %s\? .* will be deducted from your wallet\."
            % re.escape(shipment.name),
        )

    def test_portal_cancel_draft_no_wallet_credit(self):
        _order, shipment = self._create_draft()
        balance = self.wallet.balance
        self.authenticate(self.portal_login, self.portal_login)
        page = self.url_open('/my/shipments')
        csrf = self._csrf(page.text)
        result = self.url_open(
            '/my/shipments/%s/cancel' % shipment.id,
            data={'csrf_token': csrf, 'redirect': '/my/shipments'},
        )
        self.assertEqual(result.status_code, 200)
        shipment.invalidate_recordset()
        self.assertEqual(shipment.state, 'cancelled')
        credits = self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', 'IP-CANCEL:%s' % shipment.id),
        ])
        self.assertFalse(credits)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, balance, places=2)

    def test_portal_edit_rejected_after_pickup_requested(self):
        _order, shipment = self._create_draft()
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'pickup_requested',
        })
        self.authenticate(self.portal_login, self.portal_login)
        page = self.url_open('/my/shipments/%s/edit' % shipment.id)
        # Redirects away from the edit form with an error flash.
        self.assertNotIn('Edit Shipment:', page.text)
        list_page = self.url_open('/my/shipments')
        self.assertNotIn('/my/shipments/%s/edit' % shipment.id, list_page.text)
        # Pre-scan cancel is India Post only; own-network pickup_requested
        # has neither Edit nor draft Cancel.
        self.assertNotRegex(
            list_page.text,
            r'action="/my/shipments/%s/cancel"' % shipment.id,
        )
