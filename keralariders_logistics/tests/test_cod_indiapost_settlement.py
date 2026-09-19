"""India Post COD reaches the seller's ledger, and the payout says so.

India Post collects the cash and remits it to the company, so a delivered COD
article has to credit the seller without any DE or hub custody leg. Delivery
arrives twice in practice — a tracking poll and a hand-marked delivery — and a
poll repeats, so the credit is asserted to happen exactly once whichever path
ran. The payout side covers the two mails (team on request, seller on approval
with the 24-hour promise) and the finance Mark Paid stamp.
"""

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

ADMIN_GROUP = 'keralariders_logistics.group_logistics_admin'
OPS_PARAM = 'keralariders_logistics.ops_notification_email'


@tagged('post_install', '-at_install')
class TestIndiapostCodSettlement(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.Transfer = cls.env['logistics.account.transfer']
        cls.Mail = cls.env['mail.mail']
        cls.env['ir.config_parameter'].sudo().set_param(
            'web.base.url', 'https://erp.keralaxpress.com')
        cls.env.company.sudo().write({'email': 'notifications@keralaxpress.com'})
        cls.admin_user = cls.env['res.users'].create({
            'name': 'COD Settlement Admin',
            'login': 'kx_cod_settlement_admin',
            'email': 'kx.cod.admin@example.com',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref(ADMIN_GROUP).id,
            ])],
        })
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'India Post COD Seller',
            'email': 'ipcod.seller@example.com',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(OPS_PARAM, '')

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------
    def _new_ip_cod_shipment(self, article='EY547878537IN', cod=299.0,
                             state='out_for_delivery', **overrides):
        vals = {
            'seller_id': self.seller.id,
            'shipping_to_name': 'COD Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'COD article',
            'total_weight': 0.4,
            'length_cm': 20,
            'breadth_cm': 15,
            'height_cm': 10,
            'order_payment_type': 'cod',
            'total_order_value': cod,
            'cod_amount': cod,
        }
        vals.update(overrides)
        shipment = self.env['logistics.shipment'].create(vals)
        shipment.sudo().with_context(allow_shipment_state_write=True).write({
            'indiapost_article_number': article,
            'state': state,
        })
        return shipment

    def _deliver(self, shipment):
        shipment.sudo()._write_with_state({
            'state': 'delivered',
            'custodian_type': 'customer',
            'delivered_on': fields.Datetime.now(),
        })

    def _cod_payments(self, shipment):
        return self.Transfer.sudo().search([
            ('transfer_type', '=', 'cod_payment'),
            ('shipment_id', '=', shipment.id),
        ])

    def _outgoing(self, subject_part=None, email_part=None):
        mails = self.Mail.sudo().search([('state', '=', 'outgoing')], order='id')
        if subject_part:
            mails = mails.filtered(
                lambda m: subject_part.lower() in (m.subject or '').lower())
        if email_part:
            mails = mails.filtered(
                lambda m: email_part.lower() in (m.email_to or '').lower())
        return mails

    def _withdrawable_seller(self):
        self.seller.sudo().write({
            'bank_account_name': 'India Post COD Seller',
            'bank_account_number': '123456789012',
            'bank_ifsc': 'HDFC0001234',
            'bank_name': 'HDFC Bank',
        })
        return self.seller

    # ------------------------------------------------------------------
    # Credit on delivery
    # ------------------------------------------------------------------
    def test_delivered_indiapost_cod_credits_the_seller_once(self):
        shipment = self._new_ip_cod_shipment()
        self.assertFalse(self._cod_payments(shipment))

        self._deliver(shipment)

        payments = self._cod_payments(shipment)
        self.assertEqual(len(payments), 1, payments.mapped('name'))
        self.assertEqual(payments.amount, 299.0)
        self.assertEqual(payments.state, 'posted')
        self.assertEqual(payments.related_seller_id, self.seller)
        self.assertEqual(
            payments.to_account_id,
            self.env['logistics.account'].get_company_cod_account(),
            'India Post remits to the company, so the credit lands there',
        )
        self.assertEqual(payments.from_account_id.account_type, 'cod_customer')
        self.assertTrue(shipment.indiapost_cod_credited)
        self.assertIn(payments, shipment.cod_payment_transfer_ids)
        self.assertEqual(
            self.Transfer.get_seller_cod_pending_balance(self.seller), 299.0)

    def test_repeat_delivered_write_does_not_double_credit(self):
        shipment = self._new_ip_cod_shipment(article='EY547878545IN')
        self._deliver(shipment)
        self.assertEqual(len(self._cod_payments(shipment)), 1)

        # A later tracking poll re-applies the same terminal state.
        shipment.sudo()._write_with_state({'state': 'delivered'})
        shipment.sudo()._write_with_state({'state': 'delivered'})

        self.assertEqual(len(self._cod_payments(shipment)), 1)
        self.assertEqual(
            self.Transfer.get_seller_cod_pending_balance(self.seller), 299.0)

    def test_tracking_sync_delivered_scan_credits_the_seller(self):
        shipment = self._new_ip_cod_shipment(
            article='EY547878553IN', state='in_transit')
        self.env['logistics.indiapost.tracking']._ip_apply_tracking(shipment, {
            'del_status': 'Delivered',
            'tracking_details': [{
                'date': '2026-09-16T00:00:00Z',
                'time': '10:06:00',
                'event': 'Item Delivered',
                'office': 'Kochi HO',
                'officeid': '22360020',
                'eventId': 'ip-delivered-1',
            }],
        })
        self.assertEqual(shipment.state, 'delivered')
        self.assertEqual(len(self._cod_payments(shipment)), 1)

    def test_manual_mark_delivered_credits_the_seller(self):
        shipment = self._new_ip_cod_shipment(article='EY547878561IN')
        shipment.sudo().action_mark_delivered()
        self.assertEqual(shipment.state, 'delivered')
        self.assertEqual(len(self._cod_payments(shipment)), 1)

    def test_prepaid_and_own_network_deliveries_are_not_credited(self):
        prepaid = self._new_ip_cod_shipment(
            article='EY547878579IN', cod=0.0, order_payment_type='prepaid')
        self._deliver(prepaid)
        self.assertFalse(self._cod_payments(prepaid))

        own = self._new_ip_cod_shipment(article='EY547878587IN')
        own.sudo().with_context(allow_fulfilment_method_write=True).write(
            {'fulfilment_method': 'own_network'})
        self._deliver(own)
        self.assertFalse(
            self._cod_payments(own),
            'own-network COD is raised by the DE settlement, not here',
        )

    def test_backfill_credits_delivered_shipments_and_is_repeatable(self):
        shipment = self._new_ip_cod_shipment(article='EY547878595IN')
        # A shipment delivered before this code existed: state written with the
        # credit hook bypassed, exactly as the historical rows look.
        shipment.sudo().with_context(
            allow_shipment_state_write=True,
        ).write({'state': 'delivered'})
        shipment.sudo().indiapost_cod_credited = False
        self._cod_payments(shipment).sudo().unlink()
        self.assertFalse(self._cod_payments(shipment))

        credited = self.env['logistics.shipment']._ip_backfill_cod_credits()
        self.assertIn(shipment, credited)
        self.assertEqual(len(self._cod_payments(shipment)), 1)

        self.env['logistics.shipment']._ip_backfill_cod_credits()
        self.assertEqual(
            len(self._cod_payments(shipment)), 1,
            'a second backfill run must not credit the shipment again',
        )

    def test_backfill_stamps_shipments_that_already_have_a_payment(self):
        shipment = self._new_ip_cod_shipment(article='EY547878603IN')
        self._deliver(shipment)
        shipment.sudo().indiapost_cod_credited = False

        self.env['logistics.shipment']._ip_backfill_cod_credits()

        self.assertTrue(shipment.indiapost_cod_credited)
        self.assertEqual(len(self._cod_payments(shipment)), 1)

    # ------------------------------------------------------------------
    # Withdrawal mails and payout
    # ------------------------------------------------------------------
    def test_withdrawal_request_queues_a_team_mail(self):
        self.env['ir.config_parameter'].sudo().set_param(
            OPS_PARAM, 'kx.ops.team@example.com')
        shipment = self._new_ip_cod_shipment(article='EY547878611IN')
        self._deliver(shipment)
        seller = self._withdrawable_seller()

        transfer = self.Transfer.action_create_cod_withdrawal(seller, 299.0)

        self.assertEqual(transfer.state, 'draft')
        self.assertEqual(transfer.cod_payout_state, 'requested')
        mails = self._outgoing('COD withdrawal pending approval',
                               'kx.ops.team@example.com')
        mails = mails.filtered(lambda m: transfer.name in (m.subject or ''))
        self.assertEqual(len(mails), 1, mails.mapped('subject'))
        self.assertEqual(mails.state, 'outgoing', 'mail must stay queued')
        self.assertIn('notifications@', (mails.email_from or '').lower())
        body = '%s %s' % (mails.body_html or '', mails.body or '')
        self.assertIn('India Post COD Seller', body)
        self.assertIn('123456789012', body, 'bank reference belongs in the mail')
        self.assertIn('erp.keralaxpress.com', body, 'backend link is missing')
        self.assertIn('ipcod.seller@example.com', (mails.reply_to or '').lower())

    def test_approval_mails_the_seller_the_24_hour_promise(self):
        shipment = self._new_ip_cod_shipment(article='EY547878629IN')
        self._deliver(shipment)
        seller = self._withdrawable_seller()
        transfer = self.Transfer.action_create_cod_withdrawal(seller, 299.0)

        transfer.action_approve()

        self.assertEqual(transfer.state, 'posted')
        self.assertEqual(transfer.cod_payout_state, 'approved')
        mails = self._outgoing('COD withdrawal approved',
                               'ipcod.seller@example.com')
        mails = mails.filtered(lambda m: transfer.name in (m.subject or ''))
        self.assertEqual(len(mails), 1, mails.mapped('subject'))
        self.assertIn('notifications@', (mails.email_from or '').lower())
        body = '%s %s' % (mails.body_html or '', mails.body or '')
        self.assertIn('24 hours', body)
        self.assertIn('299', body)
        self.assertEqual(
            self.Transfer.get_seller_cod_pending_balance(seller), 0.0,
            'an approved withdrawal settles the pending balance',
        )

        with self.assertRaises(UserError):
            transfer.action_approve()
        self.assertEqual(
            len(self._outgoing('COD withdrawal approved',
                               'ipcod.seller@example.com').filtered(
                lambda m: transfer.name in (m.subject or ''))),
            1,
            'approval must not mail the seller twice',
        )

    def test_mark_paid_stamps_who_and_when(self):
        shipment = self._new_ip_cod_shipment(article='EY547878637IN')
        self._deliver(shipment)
        seller = self._withdrawable_seller()
        transfer = self.Transfer.action_create_cod_withdrawal(seller, 299.0)

        with self.assertRaises(UserError):
            transfer.action_mark_cod_paid()

        transfer.action_approve()
        transfer.with_user(self.admin_user).action_mark_cod_paid()

        self.assertEqual(transfer.cod_payout_state, 'paid')
        self.assertTrue(transfer.cod_paid_on)
        self.assertEqual(transfer.cod_paid_by, self.admin_user)
        self.assertEqual(
            transfer.state, 'posted',
            'marking paid must not unpost the ledger lines',
        )
        self.assertTrue(transfer.transaction_ids)

        stamped = transfer.cod_paid_on
        transfer.action_mark_cod_paid()
        self.assertEqual(transfer.cod_paid_on, stamped)
