from datetime import date

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from .common import OCT_1, OCT_31, KxFinanceFixtures


@tagged('post_install', '-at_install')
class TestKxFinance(KxFinanceFixtures, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._kx_setup_fixtures()
        cls.service = cls.env['kx.report.service']
        cls.Invoice = cls.env['kx.seller.invoice']

    # ------------------------------------------------------------------
    # Ledger
    # ------------------------------------------------------------------
    def _ledger(self, seller):
        return self.env['kx.seller.ledger.line'].search(
            [('seller_id', '=', seller.id)], order='date asc, id asc')

    def test_ledger_categories(self):
        lines = self._ledger(self.seller_a)
        by_ref = {l.reference: l.category for l in lines if l.source == 'wallet'}
        self.assertEqual(by_ref[self.recharge_line.reference], 'recharge')
        self.assertEqual(by_ref[self.s1.display_name], 'booking')
        self.assertEqual(by_ref['IP-CANCEL:%s' % self.s2.id], 'cancel_refund')
        self.assertEqual(by_ref['IP-SCAN-ADJ:%s' % self.s1.id], 'scan_adjustment')
        self.assertEqual(by_ref['Manual adjustment'], 'manual')
        cod = lines.filtered(lambda l: l.source == 'cod')
        self.assertEqual(
            sorted(cod.mapped('category')),
            ['cod_charge', 'cod_collected', 'cod_payout'])
        charge = cod.filtered(lambda l: l.category == 'cod_charge')
        payout = cod.filtered(lambda l: l.category == 'cod_payout')
        self.assertAlmostEqual(charge.debit, 8.0)
        self.assertAlmostEqual(payout.debit, 392.0)

    def test_ledger_running_balances_match_live_balances(self):
        lines = self._ledger(self.seller_a)
        last = lines[-1]
        self.wallet_a.invalidate_recordset(['balance'])
        self.assertAlmostEqual(last.wallet_balance, self.wallet_a.balance)
        pending = self.Transfer.get_seller_cod_pending_balance(self.seller_a)
        self.assertAlmostEqual(last.cod_balance, pending)
        self.assertAlmostEqual(pending, 100.0)
        # Wallet lines never move the COD balance and the other way round.
        for line in lines:
            if line.source == 'wallet':
                self.assertEqual(line.cod_amount, 0.0)
            else:
                self.assertEqual(line.wallet_amount, 0.0)

    def test_ledger_statement_opening_and_closing(self):
        st = self.service.ledger_statement(
            self.seller_a, date(2026, 10, 5), OCT_31)
        # Before 5 Oct: +1000 -100 -150 +150 = 900 wallet, no COD yet.
        self.assertAlmostEqual(st['opening_wallet'], 900.0)
        self.assertAlmostEqual(st['opening_cod'], 0.0)
        self.assertAlmostEqual(st['closing_wallet'], 875.0)
        self.assertAlmostEqual(st['closing_cod'], 100.0)
        only_cod = self.service.ledger_statement(
            self.seller_a, OCT_1, OCT_31, source='cod')
        self.assertEqual(len(only_cod['lines']), 3)
        self.assertAlmostEqual(only_cod['closing_wallet'], 875.0)

    def test_ledger_exports(self):
        xlsx = self.service.ledger_statement_xlsx(self.seller_a, OCT_1, OCT_31)
        self.assertTrue(xlsx.startswith(b'PK'))
        html, _kind = self.env['ir.actions.report']._render_qweb_html(
            'keralaxpress_finance.action_report_ledger_statement', self.seller_a.ids,
            data={'date_from': '2026-10-01', 'date_to': '2026-10-31', 'source': False})
        self.assertIn(b'Ledger Statement', html)
        self.assertIn(b'COD settlement charge', html)

    # ------------------------------------------------------------------
    # COD wallet
    # ------------------------------------------------------------------
    def test_cod_report(self):
        data = self.service.cod_report(OCT_1, OCT_31, [self.seller_a.id, self.seller_b.id])
        self.assertAlmostEqual(data['total_balance'], 100.0)
        row = next(r for r in data['seller_rows'] if r['seller_id'] == self.seller_a.id)
        self.assertAlmostEqual(row['credit'], 500.0)
        self.assertAlmostEqual(row['debit'], 400.0)
        self.assertEqual(
            [(r['date'], r['closing']) for r in data['day_rows']],
            [(date(2026, 10, 5), 500.0), (date(2026, 10, 10), 100.0)])
        later = self.service.cod_report(date(2026, 10, 6), OCT_31, [self.seller_a.id])
        self.assertAlmostEqual(later['opening_total'], 500.0)
        self.assertAlmostEqual(
            self.service.seller_cod_balance(self.seller_a),
            self.Transfer.get_seller_cod_pending_balance(self.seller_a))
        self.assertTrue(self.service.cod_report_xlsx(OCT_1, OCT_31).startswith(b'PK'))

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def test_order_report(self):
        data = self.service.order_report(OCT_1, OCT_31, [self.seller_a.id])
        totals = data['totals']
        self.assertEqual(totals['count'], 3)
        self.assertAlmostEqual(totals['value'], 1700.0)
        self.assertEqual(totals['delivered'], 1)
        self.assertEqual(totals['cancelled'], 1)
        self.assertEqual(
            {r['state']: r['count'] for r in data['status_rows']},
            {'delivered': 1, 'cancelled': 1, 'pickup_requested': 1})

        both = self.service.order_report(
            OCT_1, OCT_31, [self.seller_a.id, self.seller_b.id], ['delivered'])
        self.assertEqual(both['totals']['count'], 2)
        self.assertEqual(
            {r['seller_id']: r['delivered'] for r in both['seller_rows']},
            {self.seller_a.id: 1, self.seller_b.id: 1})
        self.assertEqual(
            self.service.order_report(date(2026, 11, 1), date(2026, 11, 30),
                                      [self.seller_a.id])['totals']['count'], 0)
        self.assertTrue(self.service.order_report_xlsx(
            OCT_1, OCT_31, [self.seller_a.id]).startswith(b'PK'))

    # ------------------------------------------------------------------
    # Invoices
    # ------------------------------------------------------------------
    def _generate(self, **kw):
        return self.Invoice.generate_for_period(
            OCT_1, OCT_31, sellers=self.seller_a | self.seller_b, **kw)

    def test_invoice_amounts_intra_state(self):
        invoices = self._generate()
        inv = invoices.filtered(lambda i: i.seller_id == self.seller_a)
        self.assertEqual(inv.state, 'posted')
        self.assertTrue(inv.name.startswith('KXINV/2026/10/'))
        self.assertAlmostEqual(inv.recharge_total, 1000.0)
        self.assertEqual(inv.booking_count, 2)
        self.assertAlmostEqual(inv.shipment_charges, 250.0)
        # Rate adjustment 20 + manual 5 + COD settlement charge 8.
        self.assertAlmostEqual(inv.additional_charges, 33.0)
        self.assertAlmostEqual(inv.refunds, 150.0)
        self.assertAlmostEqual(inv.amount_total, 133.0)
        self.assertAlmostEqual(inv.taxable_amount, 112.71)
        self.assertAlmostEqual(inv.tax_amount, 20.29)
        self.assertEqual(inv.tax_mode, 'intra')
        self.assertAlmostEqual(inv.cgst_amount + inv.sgst_amount, inv.tax_amount)
        self.assertLessEqual(abs(inv.cgst_amount - inv.sgst_amount), 0.011)
        self.assertEqual(inv.igst_amount, 0.0)
        self.assertEqual(len(inv.line_ids), 1)
        self.assertEqual(inv.line_ids.line_key, 'wallet_charge')
        self.assertAlmostEqual(inv.line_ids.amount, 133.0)
        self.assertAlmostEqual(sum(inv.line_ids.mapped('amount')), 133.0)
        html, _kind = self.env['ir.actions.report']._render_qweb_html(
            'keralaxpress_finance.action_report_seller_invoice', inv.ids)
        self.assertIn(b'Tax Invoice', html)

    def test_invoice_inter_state_uses_igst(self):
        inv = self._generate().filtered(lambda i: i.seller_id == self.seller_b)
        self.assertEqual(inv.tax_mode, 'inter')
        self.assertAlmostEqual(inv.amount_total, 100.0)
        self.assertAlmostEqual(inv.igst_amount, inv.tax_amount)
        self.assertEqual(inv.cgst_amount + inv.sgst_amount, 0.0)

    def test_invoice_generation_is_idempotent(self):
        first = self._generate()
        self.assertEqual(len(first), 2)
        self.assertFalse(self._generate())
        again = self._generate(regenerate=True)
        self.assertEqual(len(again), 2)
        self.assertEqual(set(first.mapped('state')), {'cancelled'})
        active = self.Invoice.search([
            ('seller_id', 'in', (self.seller_a | self.seller_b).ids),
            ('date_from', '=', OCT_1), ('state', '!=', 'cancelled')])
        self.assertEqual(active, again)

    def test_posted_invoice_is_frozen(self):
        inv = self._generate().filtered(lambda i: i.seller_id == self.seller_a)
        with self.assertRaises(UserError):
            inv.write({'amount_total': 1.0})

    def test_invoice_wizard_and_cron(self):
        wizard = self.env['kx.invoice.generate.wizard'].create({
            'month': '10', 'year': 2026,
            'seller_ids': [(6, 0, self.seller_a.ids)], 'post': False,
        })
        action = wizard.action_generate()
        inv = self.Invoice.search(action['domain'])
        self.assertEqual(inv.state, 'draft')
        self.assertEqual(inv.name, '/')
        inv.action_post()
        self.assertNotEqual(inv.name, '/')
        # The cron only bills the previous month, so it never touches October here.
        self.Invoice._cron_generate_previous_month()

    # ------------------------------------------------------------------
    # Expenses
    # ------------------------------------------------------------------
    def test_expense_rights(self):
        staff = self.env['res.users'].create({
            'name': 'Finance Staff', 'login': 'kx_fin_staff',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('keralariders_logistics.group_staff_finance').id,
            ])],
        })
        category = self.env.ref('keralaxpress_finance.expense_category_fuel')
        expense = self.env['kx.expense'].with_user(staff).create({
            'category_id': category.id,
            'description': 'Diesel for pickup van',
            'amount': 2500.0,
            'date': date(2026, 10, 12),
        })
        self.assertEqual(expense.state, 'draft')
        self.assertTrue(expense.name.startswith('EXP/'))
        with self.assertRaises(AccessError):
            expense.action_approve()
        expense.sudo(False).with_user(self.env.ref('base.user_admin')).action_approve()
        self.assertEqual(expense.state, 'approved')
        with self.assertRaises(AccessError):
            expense.write({'amount': 1.0})
        xlsx = self.env['kx.expense'].expense_report_xlsx(OCT_1, OCT_31)
        self.assertTrue(xlsx.startswith(b'PK'))
