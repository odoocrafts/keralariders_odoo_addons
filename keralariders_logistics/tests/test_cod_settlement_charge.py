"""COD settlement charge on seller withdrawals.

The seller asks to settle a gross amount; the company keeps a configurable
percent and pays the rest to their bank. The gross is what posts to the ledger
and leaves the pending balance, so the charge must never be deducted twice.
The percent is frozen on the request, and approval promises the net within
24 hours rather than on the settlement-cycle date.
"""

import re
from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.models.account import (
    COD_SETTLEMENT_CHARGE_PARAM,
    DEFAULT_COD_SETTLEMENT_CHARGE_PERCENT,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

OPS_PARAM = 'keralariders_logistics.ops_notification_email'


@tagged('post_install', '-at_install')
class TestCodSettlementCharge(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=False)
        cls.Transfer = cls.env['logistics.account.transfer']
        cls.Mail = cls.env['mail.mail']
        cls.ICP = cls.env['ir.config_parameter'].sudo()
        cls.env.company.sudo().write({'email': 'notifications@keralaxpress.com'})
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'COD Charge Seller',
            'email': 'cod.charge.seller@example.com',
            'zip': '682001',
            'bank_account_name': 'COD Charge Seller',
            'bank_account_number': '555566667777',
            'bank_ifsc': 'SBIN0004321',
            'bank_name': 'SBI',
        })

    def setUp(self):
        super().setUp()
        self.ICP.set_param(COD_SETTLEMENT_CHARGE_PARAM, '2.0')
        self.ICP.set_param(OPS_PARAM, '')

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------
    def _credit_cod(self, amount):
        Account = self.env['logistics.account']
        return self.Transfer.sudo().create({
            'transfer_type': 'cod_payment',
            'state': 'posted',
            'from_account_id': Account.get_indiapost_cod_account().id,
            'to_account_id': Account.get_company_cod_account().id,
            'amount': amount,
            'related_seller_id': self.seller.id,
        })

    def _withdraw(self, amount, request_date=date(2026, 9, 7)):
        with patch.object(
            type(self.Transfer),
            '_cod_withdrawal_request_date',
            return_value=request_date,
        ):
            return self.Transfer.action_create_cod_withdrawal(self.seller, amount)

    def _outgoing(self, subject_part, email_part, transfer):
        mails = self.Mail.sudo().search([('state', '=', 'outgoing')], order='id')
        return mails.filtered(
            lambda m: subject_part.lower() in (m.subject or '').lower()
            and email_part.lower() in (m.email_to or '').lower()
            and transfer.name in (m.subject or '')
        )

    # ------------------------------------------------------------------
    # Amounts
    # ------------------------------------------------------------------
    def test_two_percent_of_598(self):
        breakdown = self.Transfer.get_cod_withdrawal_breakdown(598.0)
        self.assertEqual(breakdown['percent'], 2.0)
        self.assertEqual(breakdown['percent_label'], '2%')
        self.assertAlmostEqual(breakdown['charge'], 11.96, places=2)
        self.assertAlmostEqual(breakdown['net'], 586.04, places=2)

        self._credit_cod(598.0)
        transfer = self._withdraw(598.0)
        self.assertEqual(transfer.amount, 598.0, 'amount stays the gross')
        self.assertEqual(transfer.cod_charge_percent, 2.0)
        self.assertAlmostEqual(transfer.cod_charge_amount, 11.96, places=2)
        self.assertAlmostEqual(transfer.cod_net_amount, 586.04, places=2)
        self.assertEqual(transfer.cod_charge_percent_label, '2%')

    def test_charge_rounds_half_cent_up(self):
        breakdown = self.Transfer.get_cod_withdrawal_breakdown(100.25)
        # 2% of 100.25 is 2.005 → 2.01 with the currency's half-up rounding.
        self.assertAlmostEqual(breakdown['charge'], 2.01, places=2)
        self.assertAlmostEqual(breakdown['net'], 98.24, places=2)
        self.assertAlmostEqual(
            breakdown['charge'] + breakdown['net'], 100.25, places=2)

    def test_default_is_two_percent_when_unset(self):
        self.ICP.set_param(COD_SETTLEMENT_CHARGE_PARAM, False)
        self.assertEqual(DEFAULT_COD_SETTLEMENT_CHARGE_PERCENT, 2.0)
        self.assertEqual(self.Transfer._cod_settlement_charge_percent(), 2.0)
        settings = self.env['res.config.settings'].create({})
        self.assertEqual(settings.cod_settlement_charge_percent, 2.0)

    def _save_settings(self, percent):
        self.env['res.config.settings'].create({
            'indiapost_enabled': False,
            'cod_settlement_charge_percent': percent,
        }).execute()

    def test_settings_store_percent_including_zero(self):
        self._save_settings(3.5)
        self.assertEqual(self.ICP.get_param(COD_SETTLEMENT_CHARGE_PARAM), '3.5')
        self.assertEqual(self.Transfer._cod_settlement_charge_percent(), 3.5)

        # A waived charge must stay 0, not fall back to the 2% default.
        self._save_settings(0.0)
        self.assertEqual(self.Transfer._cod_settlement_charge_percent(), 0.0)
        self.assertEqual(
            self.env['res.config.settings'].create({}).cod_settlement_charge_percent,
            0.0,
        )

    def test_settings_refuse_out_of_range_percent(self):
        for bad in (-1.0, 100.0):
            with self.assertRaises(ValidationError) as err:
                self._save_settings(bad)
            self.assertIn('settlement charge', str(err.exception))

    def test_setting_change_does_not_rewrite_existing_request(self):
        self._credit_cod(1000.0)
        first = self._withdraw(598.0)
        self.assertEqual(first.cod_charge_percent, 2.0)

        self.ICP.set_param(COD_SETTLEMENT_CHARGE_PARAM, '5.0')
        first.invalidate_recordset()
        self.assertEqual(first.cod_charge_percent, 2.0)
        self.assertAlmostEqual(first.cod_charge_amount, 11.96, places=2)
        self.assertAlmostEqual(first.cod_net_amount, 586.04, places=2)

        # Finance correcting a draft amount recomputes at the frozen rate.
        first.write({'amount': 500.0})
        self.assertAlmostEqual(first.cod_charge_amount, 10.0, places=2)
        self.assertAlmostEqual(first.cod_net_amount, 490.0, places=2)

        # A request in the next cycle picks up the new rate.
        second = self._withdraw(100.0, request_date=date(2026, 9, 16))
        self.assertEqual(second.cod_charge_percent, 5.0)
        self.assertAlmostEqual(second.cod_charge_amount, 5.0, places=2)
        self.assertAlmostEqual(second.cod_net_amount, 95.0, places=2)

    def test_withdrawal_with_no_net_left_is_refused(self):
        self.ICP.set_param(COD_SETTLEMENT_CHARGE_PARAM, '99.99')
        self._credit_cod(10.0)
        with self.assertRaises(UserError):
            self._withdraw(0.01)

    def test_other_transfer_types_carry_no_charge(self):
        payment = self._credit_cod(250.0)
        self.assertFalse(payment.cod_charge_percent)
        self.assertEqual(payment.cod_charge_amount, 0.0)
        self.assertEqual(payment.cod_net_amount, 0.0)

    # ------------------------------------------------------------------
    # Ledger and pending balance
    # ------------------------------------------------------------------
    def test_pending_balance_drops_by_gross_not_gross_plus_charge(self):
        self._credit_cod(1000.0)
        transfer = self._withdraw(598.0)

        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.seller), 1000.0,
            places=2, msg='a draft request does not settle anything yet')
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_withdrawable_balance(self.seller),
            402.0, places=2)

        transfer.action_approve()

        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.seller), 402.0,
            places=2,
            msg='pending must drop by the gross 598, not 598 + 11.96')
        self.assertEqual(
            sorted(transfer.transaction_ids.mapped('amount')), [-598.0, 598.0],
            'one posted withdrawal line pair for the gross')
        settlements = self.Transfer.sudo().search([
            ('related_seller_id', '=', self.seller.id),
            ('transfer_type', 'in', ('cod_clearance', 'cod_withdrawal', 'other')),
            ('state', '=', 'posted'),
        ])
        self.assertEqual(settlements, transfer, 'no separate fee transfer')

    # ------------------------------------------------------------------
    # Mails
    # ------------------------------------------------------------------
    def test_approval_mail_has_breakdown_and_24_hours(self):
        self._credit_cod(598.0)
        transfer = self._withdraw(598.0)
        self.assertEqual(transfer.cod_settlement_date, date(2026, 9, 20))

        transfer.action_approve()

        mails = self._outgoing(
            'COD withdrawal approved', 'cod.charge.seller@example.com', transfer)
        self.assertEqual(len(mails), 1, mails.mapped('subject'))
        self.assertIn('notifications@', (mails.email_from or '').lower())
        body = '%s %s' % (mails.body_html or '', mails.body or '')
        self.assertIn('598.00', body)
        self.assertIn('2%', body)
        self.assertIn('11.96', body)
        self.assertIn('586.04', body)
        self.assertIn('Amount to be credited to your account', body)
        self.assertIn('555566667777', body)
        self.assertIn('24 hours', body.lower())
        self.assertNotIn('will be credited on', body.lower())
        self.assertNotIn('20 September 2026', body)

        with self.assertRaises(UserError):
            transfer.action_approve()
        self.assertEqual(
            len(self._outgoing(
                'COD withdrawal approved', 'cod.charge.seller@example.com',
                transfer)),
            1, 'approval must not mail the seller twice')

    def test_team_mail_has_breakdown_and_settlement_date(self):
        self.ICP.set_param(OPS_PARAM, 'kx.charge.ops@example.com')
        self._credit_cod(598.0)
        transfer = self._withdraw(598.0)

        mails = self._outgoing(
            'COD withdrawal pending approval', 'kx.charge.ops@example.com',
            transfer)
        self.assertEqual(len(mails), 1, mails.mapped('subject'))
        body = '%s %s' % (mails.body_html or '', mails.body or '')
        self.assertIn('598.00', body)
        self.assertIn('11.96', body)
        self.assertIn('586.04', body)
        self.assertIn('Settlement date', body)
        self.assertIn('20 September 2026', body)
        self.assertIn('555566667777', body)

    # ------------------------------------------------------------------
    # Portal
    # ------------------------------------------------------------------
    def test_portal_template_shows_breakdown(self):
        view = self.env.ref('keralariders_logistics.portal_my_cod_settlements')
        arch = view.arch_db or ''
        for needle in (
            'codWithdrawBreakdown',
            "withdrawal_preview['charge']",
            "withdrawal_preview['net']",
            'Amount to your account',
            'wr.cod_charge_amount',
            'wr.cod_net_amount',
            'wr.cod_settlement_date',
            'credited in the next 24 hours',
            'You have not requested a COD withdrawal yet',
        ):
            self.assertIn(needle, arch)
        self.assertNotIn('will be credited on', arch)


@tagged('post_install', '-at_install')
class TestCodSettlementChargePortal(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=False)
        cls.env['ir.config_parameter'].sudo().set_param(
            COD_SETTLEMENT_CHARGE_PARAM, '2.0')
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Portal Charge Seller',
            'email': 'portal.charge.seller@example.com',
            'zip': '682001',
            'bank_account_name': 'Portal Charge Seller',
            'bank_account_number': '111122223333',
            'bank_ifsc': 'HDFC0001111',
            'bank_name': 'HDFC Bank',
        })
        cls.login = 'kx_portal_cod_charge'
        cls.env['res.users'].create({
            'name': 'Portal Charge Seller',
            'login': cls.login,
            'password': cls.login,
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        Account = cls.env['logistics.account']
        cls.env['logistics.account.transfer'].sudo().create({
            'transfer_type': 'cod_payment',
            'state': 'posted',
            'from_account_id': Account.get_indiapost_cod_account().id,
            'to_account_id': Account.get_company_cod_account().id,
            'amount': 1000.0,
            'related_seller_id': cls.seller.id,
        })

    def test_portal_lists_requests_with_breakdown(self):
        self.authenticate(self.login, self.login)
        page = self.url_open('/my/cod_settlements')
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn('Withdrawal Requests', html)
        self.assertIn('You have not requested a COD withdrawal yet', html)
        # Modal preview for the full 1000.00 available.
        self.assertIn('codWithdrawBreakdown', html)
        self.assertIn('Settlement charge (2%)', html)
        self.assertIn('20.00', html)
        self.assertIn('980.00', html)

        csrf = re.search(
            r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html).group(1)
        self.url_open('/my/cod_settlements/withdraw', data={
            'csrf_token': csrf,
            'amount': '598.00',
        })

        transfer = self.env['logistics.account.transfer'].sudo().search([
            ('related_seller_id', '=', self.seller.id),
            ('transfer_type', '=', 'cod_withdrawal'),
        ])
        self.assertEqual(len(transfer), 1)
        self.assertAlmostEqual(transfer.cod_net_amount, 586.04, places=2)

        html = self.url_open('/my/cod_settlements').text
        self.assertNotIn('You have not requested a COD withdrawal yet', html)
        self.assertIn(transfer.name, html)
        self.assertIn('598.00', html)
        self.assertIn('11.96', html)
        self.assertIn('586.04', html)
        self.assertIn('Amount to your account', html)
        self.assertIn('Awaiting approval', html)
        self.assertIn('Settlement date', html)

        transfer.action_approve()
        html = self.url_open('/my/cod_settlements').text
        self.assertIn('Approved — credited in the next 24 hours', html)
