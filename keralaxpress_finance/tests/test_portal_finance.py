from odoo.tests import HttpCase, tagged

from .common import OCT_1, OCT_31, KxFinanceFixtures


@tagged('post_install', '-at_install')
class TestKxFinancePortal(KxFinanceFixtures, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._kx_setup_fixtures()
        cls.env['res.users'].create({
            'name': 'Finance Seller A',
            'login': 'kx_fin_portal_a',
            'password': 'kx_fin_portal_a',
            'partner_id': cls.seller_a.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        invoices = cls.env['kx.seller.invoice'].generate_for_period(
            OCT_1, OCT_31, sellers=cls.seller_a | cls.seller_b)
        cls.invoice_a = invoices.filtered(lambda i: i.seller_id == cls.seller_a)
        cls.invoice_b = invoices.filtered(lambda i: i.seller_id == cls.seller_b)

    def setUp(self):
        super().setUp()
        self.authenticate('kx_fin_portal_a', 'kx_fin_portal_a')

    def test_reports_page_shows_own_data(self):
        res = self.url_open('/my/reports?date_from=2026-10-01&date_to=2026-10-31')
        self.assertEqual(res.status_code, 200)
        self.assertIn('Order status summary', res.text)
        res = self.url_open('/my/reports/export?report=orders&date_from=2026-10-01&date_to=2026-10-31')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.content.startswith(b'PK'))
        res = self.url_open('/my/reports/export?report=cod&date_from=2026-10-01&date_to=2026-10-31')
        self.assertTrue(res.content.startswith(b'PK'))

    def test_ledger_is_seller_scoped(self):
        res = self.url_open('/my/ledger?date_from=2026-10-01&date_to=2026-10-31&seller_id=%s'
                            % self.seller_b.id)
        self.assertEqual(res.status_code, 200)
        self.assertIn(self.s1.display_name, res.text)
        self.assertNotIn(self.s4.display_name, res.text)
        res = self.url_open('/my/ledger/export?file_format=xlsx&date_from=2026-10-01&date_to=2026-10-31')
        self.assertTrue(res.content.startswith(b'PK'))
        res = self.url_open('/my/ledger/export?file_format=pdf&date_from=2026-10-01&date_to=2026-10-31')
        self.assertEqual(res.status_code, 200)

    def test_invoices_are_seller_scoped(self):
        res = self.url_open('/my/kx_invoices')
        self.assertIn(self.invoice_a.name, res.text)
        self.assertNotIn(self.invoice_b.name, res.text)
        own = self.url_open('/my/kx_invoices/%s/pdf' % self.invoice_a.id, allow_redirects=False)
        self.assertEqual(own.status_code, 200)
        other = self.url_open('/my/kx_invoices/%s/pdf' % self.invoice_b.id, allow_redirects=False)
        self.assertIn(other.status_code, (301, 302, 303))

    def test_portal_rule_hides_other_invoices(self):
        portal_user = self.env['res.users'].search([('login', '=', 'kx_fin_portal_a')])
        visible = self.env['kx.seller.invoice'].with_user(portal_user).search([])
        self.assertEqual(visible, self.invoice_a)
