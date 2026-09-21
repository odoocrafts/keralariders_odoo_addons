"""COD withdrawal settlement-cycle: fixed payout dates, one open request per cycle."""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

ADMIN_GROUP = 'keralariders_logistics.group_logistics_admin'


@tagged('post_install', '-at_install')
class TestCodWithdrawalSettlementCycle(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.Transfer = cls.env['logistics.account.transfer']
        cls.Mail = cls.env['mail.mail']
        cls.env.company.sudo().write({'email': 'notifications@keralaxpress.com'})
        cls.admin_user = cls.env['res.users'].create({
            'name': 'COD Cycle Admin',
            'login': 'kx_cod_cycle_admin',
            'email': 'kx.cod.cycle.admin@example.com',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref(ADMIN_GROUP).id,
            ])],
        })
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'COD Cycle Seller',
            'email': 'cod.cycle.seller@example.com',
            'zip': '682001',
            'bank_account_name': 'COD Cycle Seller',
            'bank_account_number': '987654321098',
            'bank_ifsc': 'SBIN0001234',
            'bank_name': 'SBI',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})

    def _credit_cod(self, article, amount=100.0):
        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'Cycle Customer',
            'shipping_to_address': '1 Cycle Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Cycle COD',
            'total_weight': 0.4,
            'length_cm': 20,
            'breadth_cm': 15,
            'height_cm': 10,
            'order_payment_type': 'cod',
            'total_order_value': amount,
            'cod_amount': amount,
        })
        shipment.sudo().with_context(allow_shipment_state_write=True).write({
            'indiapost_article_number': article,
            'state': 'out_for_delivery',
        })
        shipment.sudo()._write_with_state({
            'state': 'delivered',
            'custodian_type': 'customer',
        })
        return shipment

    def _withdraw_on(self, request_date, amount):
        with patch.object(
            type(self.Transfer),
            '_cod_withdrawal_request_date',
            return_value=request_date,
        ):
            return self.Transfer.action_create_cod_withdrawal(self.seller, amount)

    # ------------------------------------------------------------------
    # Date table
    # ------------------------------------------------------------------
    def test_settlement_date_table(self):
        settle = self.Transfer._cod_settlement_date_for_request_date
        cases = [
            (date(2026, 9, 7), date(2026, 9, 20)),
            (date(2026, 9, 6), date(2026, 9, 20)),
            (date(2026, 9, 15), date(2026, 9, 20)),
            (date(2026, 9, 16), date(2026, 9, 30)),
            (date(2026, 9, 25), date(2026, 9, 30)),
            (date(2026, 2, 20), date(2026, 2, 28)),
            (date(2028, 2, 16), date(2028, 2, 28)),  # leap year → still 28
            (date(2026, 9, 27), date(2026, 10, 10)),
            (date(2026, 10, 3), date(2026, 10, 10)),
            (date(2026, 1, 31), date(2026, 2, 10)),
            (date(2026, 2, 5), date(2026, 2, 10)),
            (date(2026, 12, 28), date(2027, 1, 10)),
            (date(2027, 1, 1), date(2027, 1, 10)),
        ]
        for request_date, expected in cases:
            self.assertEqual(
                settle(request_date), expected,
                'request %s should settle on %s' % (request_date, expected),
            )

    def test_cycle_info_summary_for_mid_month_window(self):
        info = self.Transfer.get_cod_settlement_cycle_info(
            self.seller, request_date=date(2026, 9, 7))
        self.assertEqual(info['settlement_date'], date(2026, 9, 20))
        self.assertIn('6 Sep', info['summary'])
        self.assertIn('15 Sep', info['summary'])
        self.assertIn('20 Sep', info['summary'])

    def test_settlement_date_stored_once_not_recomputed(self):
        self._credit_cod('EYCYCLE000001IN', 150.0)
        transfer = self._withdraw_on(date(2026, 9, 7), 150.0)
        self.assertEqual(transfer.cod_settlement_date, date(2026, 9, 20))
        # Later calendar day must not rewrite the stamped settlement date.
        transfer.invalidate_recordset(['cod_settlement_date'])
        self.assertEqual(transfer.cod_settlement_date, date(2026, 9, 20))

    # ------------------------------------------------------------------
    # One open request per cycle
    # ------------------------------------------------------------------
    def test_second_request_same_cycle_is_blocked(self):
        self._credit_cod('EYCYCLE000002IN', 200.0)
        first = self._withdraw_on(date(2026, 9, 7), 50.0)
        self.assertEqual(first.cod_settlement_date, date(2026, 9, 20))

        with self.assertRaises(UserError) as err:
            self._withdraw_on(date(2026, 9, 10), 50.0)
        self.assertIn('settlement on', str(err.exception).lower())
        self.assertIn(first.name, str(err.exception))

    def test_approved_unpaid_still_blocks_same_cycle(self):
        self._credit_cod('EYCYCLE000003IN', 200.0)
        first = self._withdraw_on(date(2026, 9, 16), 50.0)
        first.action_approve()
        self.assertEqual(first.cod_payout_state, 'approved')

        with self.assertRaises(UserError):
            self._withdraw_on(date(2026, 9, 20), 50.0)

    def test_second_cycle_allowed_after_first_is_paid(self):
        self._credit_cod('EYCYCLE000004IN', 100.0)
        first = self._withdraw_on(date(2026, 9, 7), 100.0)
        first.action_approve()
        first.with_user(self.admin_user).action_mark_cod_paid()
        self.assertEqual(first.cod_payout_state, 'paid')

        self._credit_cod('EYCYCLE000005IN', 100.0)
        second = self._withdraw_on(date(2026, 9, 16), 100.0)
        self.assertEqual(second.cod_settlement_date, date(2026, 9, 30))
        self.assertEqual(second.cod_payout_state, 'requested')

    def test_cancelled_request_does_not_block_same_cycle(self):
        self._credit_cod('EYCYCLE000006IN', 100.0)
        first = self._withdraw_on(date(2026, 10, 3), 100.0)
        self.assertEqual(first.cod_settlement_date, date(2026, 10, 10))
        first.action_cancel_draft()

        second = self._withdraw_on(date(2026, 10, 4), 100.0)
        self.assertEqual(second.cod_settlement_date, date(2026, 10, 10))

    def test_approval_mail_names_settlement_date_not_24_hours(self):
        self._credit_cod('EYCYCLE000007IN', 100.0)
        transfer = self._withdraw_on(date(2026, 9, 7), 100.0)
        transfer.action_approve()

        mails = self.Mail.sudo().search([
            ('state', '=', 'outgoing'),
            ('email_to', 'ilike', 'cod.cycle.seller@example.com'),
        ])
        mails = mails.filtered(
            lambda m: transfer.name in (m.subject or '')
            and 'approved' in (m.subject or '').lower())
        self.assertEqual(len(mails), 1, mails.mapped('subject'))
        body = '%s %s' % (mails.body_html or '', mails.body or '')
        self.assertNotIn('24 hours', body.lower())
        self.assertIn('20 September 2026', body)
        self.assertIn('will be credited on', body.lower())
