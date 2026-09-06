"""A seller must not be able to decide what their own wallet is credited.

The money-in mirror of test_delivery_charge_integrity. Portal sellers hold
read, write and create on ``logistics.wallet.recharge.request`` because raising
a top-up request is the portal recharge journey, and ``recharged_amount`` was a
stored computed field with ``readonly=False`` whose compute depended only on
``requested_amount``. Requesting 100 and then writing ``recharged_amount`` to
100000 in a second call left the compute dormant and presented an administrator
with the seller's own figure as though the system had produced it; approving
credited it verbatim.

``action_approve_request`` was the second way in: a public method, callable
over RPC by a portal seller on their own request, stopped only by portal
lacking ``create`` on ``logistics.wallet.transaction``. That is one digit in
ir.model.access.csv, so :meth:`test_a_seller_cannot_approve_even_with_the_acl_loosened`
grants the right and proves the method refuses anyway.

``sudo()`` is used throughout the portal cases because that is what the portal
controllers do, sudo() leaves ``env.user`` as the real user — which is what the
guard checks — and it also bypasses the record rules, so what is being proved
is the model guard rather than a row filter.
"""

import re

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged

ADMIN_GROUP = 'keralariders_logistics.group_logistics_admin'


class RechargeCase(TransactionCase):
    """Fixtures shared by the guard tests and the end-to-end journey test."""

    @classmethod
    def _setup_recharge_fixtures(cls, suffix):
        cls.Request = cls.env['logistics.wallet.recharge.request']
        cls.Transaction = cls.env['logistics.wallet.transaction']

        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Recharge Guard Seller %s' % suffix,
            'zip': '682001',
        })
        # Pinned to the hub network wherever a carrier can be chosen at all.
        # This file is about who may move money in and out of a wallet, not
        # about how a parcel is priced, and an India Post seller is debited off
        # the postal tariff — which would leave the shipment fixtures below
        # priced at zero and quietly weaken the money-out assertions.
        if 'fulfilment_method' in cls.env['logistics.seller']._fields:
            cls.seller.write({'fulfilment_method': 'own_network'})
        cls.wallet = cls.seller.wallet_ids[0]
        # A real internal administrator, because _get_logistics_admin_users
        # deliberately skips the superuser: without one, no approval activity
        # is ever scheduled and the notification tests would pass vacuously.
        cls.admin_user = cls.env['res.users'].create({
            'name': 'Recharge Guard Admin %s' % suffix,
            'login': 'kx_recharge_admin_%s' % suffix,
            'email': 'kx_recharge_admin_%s@example.com' % suffix,
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref(ADMIN_GROUP).id,
            ])],
        })
        cls.portal_user = cls.env['res.users'].create({
            'name': 'Recharge Guard Portal %s' % suffix,
            'login': 'kx_recharge_%s' % suffix,
            'password': 'kx_recharge_%s' % suffix,
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        # The fixtures only prove anything if the two users really do sit on
        # opposite sides of the group the guard checks.
        assert cls.env.user.has_group(ADMIN_GROUP), \
            'the test user must be a logistics administrator'
        assert not cls.portal_user.has_group(ADMIN_GROUP), \
            'the portal test user must not be a logistics administrator'

    def _new_request(self, amount=100.0, seller=None, wallet=None):
        return self.Request.create({
            'seller_id': (seller or self.seller).id,
            'wallet_id': (wallet or self.wallet).id,
            'requested_amount': amount,
        })

    def _balance(self):
        self.wallet.invalidate_recordset(['balance'])
        return self.wallet.balance


@tagged('post_install', '-at_install')
class TestWalletRechargeIntegrity(RechargeCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_recharge_fixtures('guard')

    def _as_portal(self, record):
        return record.with_user(self.portal_user)

    # ------------------------------------------------------------------
    # The hole itself: the amount that gets credited
    # ------------------------------------------------------------------
    def test_a_seller_cannot_inflate_the_amount_they_are_credited(self):
        """Request 100, then quietly restate the payout as 100000.

        The compute stays dormant because ``requested_amount`` has not moved,
        so nothing recalculates the figure away before an administrator sees it.
        """
        recharge = self._new_request(100.0)
        self.assertEqual(recharge.recharged_amount, 100.0)

        with self.assertRaises(AccessError):
            self._as_portal(recharge).write({'recharged_amount': 100000.0})
        with self.assertRaises(AccessError):
            self._as_portal(recharge).sudo().write({'recharged_amount': 100000.0})

        self.assertEqual(recharge.recharged_amount, 100.0)

    def test_a_seller_cannot_raise_a_request_that_is_already_inflated(self):
        """Portal creates run sudoed, so create needs the same guard as write."""
        with self.assertRaises(AccessError):
            self.Request.with_user(self.portal_user).sudo().create({
                'seller_id': self.seller.id,
                'wallet_id': self.wallet.id,
                'requested_amount': 100.0,
                'recharged_amount': 100000.0,
            })

    def test_a_seller_cannot_approve_their_own_request_by_writing_state(self):
        recharge = self._new_request(100.0)

        with self.assertRaises(AccessError):
            self._as_portal(recharge).write({'state': 'approved'})
        with self.assertRaises(AccessError):
            self._as_portal(recharge).sudo().write({'state': 'approved'})

        self.assertEqual(recharge.state, 'pending_approval')

    def test_a_seller_cannot_raise_a_request_that_is_already_approved(self):
        with self.assertRaises(AccessError):
            self.Request.with_user(self.portal_user).sudo().create({
                'seller_id': self.seller.id,
                'wallet_id': self.wallet.id,
                'requested_amount': 100.0,
                'state': 'approved',
            })

    def test_a_seller_cannot_point_at_a_wallet_transaction(self):
        """The link is the "already credited" sentinel.

        Setting it makes action_approve_request a no-op, and clearing it on an
        approved request would let the credit be paid out a second time.
        """
        credited = self._new_request(100.0)
        credited.action_approve_request()
        transaction = credited.wallet_transaction_id
        self.assertTrue(transaction)

        pending = self._new_request(100.0)
        with self.assertRaises(AccessError):
            self._as_portal(pending).sudo().write({
                'wallet_transaction_id': transaction.id,
            })
        self.assertFalse(pending.wallet_transaction_id)

        with self.assertRaises(AccessError):
            self._as_portal(credited).sudo().write({'wallet_transaction_id': False})
        self.assertEqual(credited.wallet_transaction_id, transaction)

    def test_a_seller_cannot_forge_the_approval_stamp(self):
        """Who approved it, and when, is what makes an approval look routine."""
        recharge = self._new_request(100.0)
        with self.assertRaises(AccessError):
            self._as_portal(recharge).sudo().write({
                'approved_by': self.env.user.id,
                'approved_date': '2026-01-01 00:00:00',
            })
        self.assertFalse(recharge.approved_by)

    # ------------------------------------------------------------------
    # The method guard, which must not rest on an ACL elsewhere
    # ------------------------------------------------------------------
    def test_a_seller_cannot_approve_even_with_the_acl_loosened(self):
        """The ACL is deliberately removed as a defence before this is asserted.

        ``action_approve_request`` was blocked only because portal lacks create
        on logistics.wallet.transaction. Granting that right here is what makes
        this a test of the method's own group check rather than of a digit in a
        CSV that an unrelated feature could flip tomorrow.
        """
        access = self.env.ref(
            'keralariders_logistics.access_wallet_transaction_portal')
        access.write({'perm_create': True})
        self.env.registry.clear_cache()
        self.addCleanup(self.env.registry.clear_cache)
        self.addCleanup(access.write, {'perm_create': False})
        # Prove the ACL really is out of the way, so a pass cannot be the ACL
        # quietly still doing the work.
        self.Transaction.with_user(self.portal_user).create({
            'wallet_id': self.wallet.id,
            'amount': 0.0,
            'reference': 'ACL probe',
        })

        recharge = self._new_request(100.0)
        opening_balance = self._balance()

        with self.assertRaises(AccessError):
            self._as_portal(recharge).action_approve_request()
        with self.assertRaises(AccessError):
            self._as_portal(recharge).sudo().action_approve_request()

        self.assertEqual(recharge.state, 'pending_approval')
        self.assertFalse(recharge.wallet_transaction_id)
        self.assertAlmostEqual(self._balance(), opening_balance, places=2)

    def test_a_seller_cannot_reopen_a_reviewed_request(self):
        recharge = self._new_request(100.0)
        recharge.action_cancel()
        self.assertEqual(recharge.state, 'cancelled')

        with self.assertRaises(AccessError):
            self._as_portal(recharge).sudo().action_reset()
        self.assertEqual(recharge.state, 'cancelled')

    # ------------------------------------------------------------------
    # Withdrawal: a seller may unmake their own paperwork, nothing more
    # ------------------------------------------------------------------
    def test_a_seller_may_withdraw_their_own_pending_request(self):
        recharge = self._new_request(100.0)

        self._as_portal(recharge).sudo().action_cancel()

        self.assertEqual(recharge.state, 'cancelled')
        self.assertFalse(recharge.wallet_transaction_id)

    def test_a_seller_cannot_withdraw_an_approved_request(self):
        """Cancelling an approved request unlinks the credit — money moves out."""
        recharge = self._new_request(100.0)
        recharge.action_approve_request()
        balance_after_credit = self._balance()

        with self.assertRaises(AccessError):
            self._as_portal(recharge).sudo().action_cancel()

        self.assertEqual(recharge.state, 'approved')
        self.assertTrue(recharge.wallet_transaction_id.exists())
        self.assertAlmostEqual(self._balance(), balance_after_credit, places=2)

    def test_a_seller_cannot_withdraw_someone_elses_request(self):
        other_seller = self.env['logistics.seller'].create({
            'name': 'Recharge Guard Bystander',
            'zip': '682001',
        })
        recharge = self._new_request(
            100.0, seller=other_seller, wallet=other_seller.wallet_ids[0])

        with self.assertRaises(AccessError):
            self._as_portal(recharge).sudo().action_cancel()
        self.assertEqual(recharge.state, 'pending_approval')

    # ------------------------------------------------------------------
    # What sellers keep: declaring what they are paying
    # ------------------------------------------------------------------
    def test_a_seller_can_still_state_what_they_are_paying(self):
        """The declaration stays the seller's while nobody has acted on it.

        And the compute must still follow it: the guard has to sit on the
        model's write() without catching the ORM writing the computed field
        back, or correcting a typo before submission would become an admin job.
        """
        recharge = self._new_request(100.0)

        self._as_portal(recharge).sudo().write({'requested_amount': 150.0})

        self.assertEqual(recharge.requested_amount, 150.0)
        self.assertEqual(recharge.recharged_amount, 150.0)

    def test_a_seller_cannot_restate_a_request_that_was_already_reviewed(self):
        """After approval the figure describes money that has already moved."""
        recharge = self._new_request(100.0)
        recharge.action_approve_request()

        with self.assertRaises(AccessError):
            self._as_portal(recharge).sudo().write({'requested_amount': 100000.0})
        self.assertEqual(recharge.requested_amount, 100.0)
        self.assertEqual(recharge.recharged_amount, 100.0)

    # ------------------------------------------------------------------
    # The ops case the guard exists to preserve
    # ------------------------------------------------------------------
    def test_an_administrator_can_correct_the_amount_down_and_approve(self):
        """Seller asks for 100, actually transfers 98, staff correct it.

        The legitimate reason recharged_amount is writable at all. Guarding it
        by refusing any divergence from requested_amount would have broken this.
        """
        recharge = self._new_request(100.0)
        opening_balance = self._balance()

        recharge.write({'recharged_amount': 98.0})
        self.assertEqual(recharge.recharged_amount, 98.0)
        self.assertEqual(recharge.requested_amount, 100.0)

        recharge.action_approve_request()

        self.assertEqual(recharge.state, 'approved')
        self.assertEqual(recharge.wallet_transaction_id.amount, 98.0)
        self.assertEqual(recharge.approved_by, self.env.user)
        self.assertTrue(recharge.approved_date)
        self.assertAlmostEqual(self._balance(), opening_balance + 98.0, places=2)

    def test_an_administrator_can_still_cancel_and_reopen(self):
        recharge = self._new_request(100.0)

        recharge.action_cancel()
        self.assertEqual(recharge.state, 'cancelled')

        recharge.action_reset()
        self.assertEqual(recharge.state, 'pending_approval')

    def test_cancelling_an_approved_request_still_reverses_the_credit(self):
        recharge = self._new_request(100.0)
        opening_balance = self._balance()
        recharge.action_approve_request()
        self.assertAlmostEqual(self._balance(), opening_balance + 100.0, places=2)

        recharge.action_cancel()

        self.assertEqual(recharge.state, 'cancelled')
        self.assertAlmostEqual(self._balance(), opening_balance, places=2)

    def test_approving_twice_credits_once(self):
        """The sentinel that stops a double click paying out twice."""
        recharge = self._new_request(100.0)
        opening_balance = self._balance()

        recharge.action_approve_request()
        credit = recharge.wallet_transaction_id
        recharge.action_approve_request()

        self.assertEqual(recharge.wallet_transaction_id, credit,
                         'the second approval created a second credit')
        self.assertAlmostEqual(self._balance(), opening_balance + 100.0, places=2)

    def test_a_zero_recharge_is_still_refused(self):
        recharge = self._new_request(100.0)
        recharge.write({'recharged_amount': 0.0})
        with self.assertRaises(UserError):
            recharge.action_approve_request()

    def test_approval_closes_the_admin_activities(self):
        """The notification behaviour has to survive the guard.

        Admins are given a To-Do per pending request; approving must still tick
        them off, or every approved request leaves an activity behind forever.
        """
        recharge = self._new_request(100.0)
        self.assertIn(self.admin_user, recharge.activity_ids.mapped('user_id'),
                      'no admin was asked to look at the pending request')

        recharge.action_approve_request()

        # Ticked off rather than deleted — the To-Do type keeps done activities
        # — so this asks whether anything is still outstanding on the request.
        recharge.invalidate_recordset(['activity_ids'])
        self.assertFalse(recharge.activity_ids,
                         'approving left the admin To-Do open')

    # ------------------------------------------------------------------
    # The money-out mirror
    # ------------------------------------------------------------------
    def test_a_seller_cannot_delete_the_wallet_transaction_for_a_shipment(self):
        """Removing a debit refunds the delivery charge — the same decision.

        Defended until now only by portal lacking unlink on
        logistics.wallet.transaction.
        """
        self.Transaction.create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'Top-up for the debit fixture',
        })
        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'Recharge Guard Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.5,
        })
        shipment.action_add_wallet_transaction()
        debit = shipment.wallet_transaction_id
        self.assertTrue(debit)
        balance_after_debit = self._balance()

        with self.assertRaises(AccessError):
            shipment.with_user(self.portal_user).sudo().delete_wallet_transaction()

        self.assertTrue(debit.exists())
        self.assertAlmostEqual(self._balance(), balance_after_debit, places=2)

    def test_a_seller_cannot_refund_themselves_by_cancelling_the_order(self):
        """The reachable route to the same refund, one call further out."""
        self.Transaction.create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'Top-up for the cancel fixture',
        })
        order = self.env['logistics.order'].create({'seller_id': self.seller.id})
        shipment = self.env['logistics.shipment'].create({
            'order_id': order.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'Recharge Guard Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.5,
        })
        shipment.action_add_wallet_transaction()
        balance_after_debit = self._balance()

        with self.assertRaises(AccessError):
            order.with_user(self.portal_user).sudo().action_cancel_order()

        self.assertTrue(shipment.wallet_transaction_id.exists())
        self.assertAlmostEqual(self._balance(), balance_after_debit, places=2)

    def test_an_administrator_can_still_remove_a_wallet_transaction(self):
        self.Transaction.create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'Top-up for the ops removal fixture',
        })
        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'Recharge Guard Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.5,
        })
        shipment.action_add_wallet_transaction()
        debit = shipment.wallet_transaction_id
        charge = -debit.amount

        shipment.delete_wallet_transaction()

        self.assertFalse(debit.exists())
        self.assertGreater(charge, 0.0)


@tagged('post_install', '-at_install')
class TestPortalRechargeJourney(HttpCase, RechargeCase):
    """The real journey, over HTTP, as a logged-in portal seller.

    A guard drawn slightly too wide would stop every seller topping up their
    wallet, which is worse than the bug. So this drives the actual routes —
    /my/wallet, /my/wallet/recharge, /my/wallet/recharge/confirm — with real
    CSRF tokens and the real one-shot confirm token, rather than calling create()
    and hoping that is what the controller does.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_recharge_fixtures('journey')
        cls.env['ir.config_parameter'].sudo().set_param(
            'keralariders_logistics.logistics_upi_id', 'keralaxpress@upi')

    def _hidden_value(self, html, name):
        match = re.search(
            r'name="%s"[^>]*\bvalue="([^"]*)"' % re.escape(name), html)
        self.assertTrue(match, 'no %s in the rendered page' % name)
        return match.group(1)

    def test_the_portal_recharge_journey_still_works(self):
        self.authenticate('kx_recharge_journey', 'kx_recharge_journey')

        wallet_page = self.url_open('/my/wallet')
        self.assertEqual(wallet_page.status_code, 200)
        self.assertIn('Request Wallet Recharge', wallet_page.text)

        pay_page = self.url_open('/my/wallet/recharge', data={
            'amount': '250.00',
            'csrf_token': self._hidden_value(wallet_page.text, 'csrf_token'),
        })
        self.assertEqual(pay_page.status_code, 200)
        self.assertIn('Scan and Pay', pay_page.text,
                      'the seller never reached the UPI QR page')

        confirmed = self.url_open('/my/wallet/recharge/confirm', data={
            'amount': '250.00',
            'recharge_token': self._hidden_value(pay_page.text, 'recharge_token'),
            'csrf_token': self._hidden_value(pay_page.text, 'csrf_token'),
        })
        self.assertEqual(confirmed.status_code, 200)

        self.env.invalidate_all()
        recharge = self.Request.search([('wallet_id', '=', self.wallet.id)])
        self.assertEqual(len(recharge), 1,
                         'the seller could not submit a recharge request')
        self.assertAlmostEqual(recharge.requested_amount, 250.0, places=2)
        self.assertAlmostEqual(recharge.recharged_amount, 250.0, places=2)
        self.assertEqual(recharge.state, 'pending_approval')
        self.assertEqual(recharge.seller_id, self.seller)

        # ...and it is an ordinary request an administrator can act on.
        opening_balance = self._balance()
        recharge.action_approve_request()
        self.assertEqual(recharge.state, 'approved')
        self.assertAlmostEqual(self._balance(), opening_balance + 250.0, places=2)

    def test_a_replayed_confirm_does_not_recharge_twice(self):
        """The double-click protection the confirm token exists for."""
        self.authenticate('kx_recharge_journey', 'kx_recharge_journey')

        wallet_page = self.url_open('/my/wallet')
        pay_page = self.url_open('/my/wallet/recharge', data={
            'amount': '250.00',
            'csrf_token': self._hidden_value(wallet_page.text, 'csrf_token'),
        })
        confirm_payload = {
            'amount': '250.00',
            'recharge_token': self._hidden_value(pay_page.text, 'recharge_token'),
            'csrf_token': self._hidden_value(pay_page.text, 'csrf_token'),
        }

        self.url_open('/my/wallet/recharge/confirm', data=confirm_payload)
        self.url_open('/my/wallet/recharge/confirm', data=confirm_payload)

        self.env.invalidate_all()
        self.assertEqual(
            self.Request.search_count([('wallet_id', '=', self.wallet.id)]), 1,
            'the replayed confirm raised a second recharge request')
