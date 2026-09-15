"""Office staff access packs: menus and ACLs follow the selected areas."""

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models.staff_access import (
    ADMIN_GROUP_XMLID,
    STAFF_ADMIN_GROUP_XMLID,
    WALLET_GROUP_XMLID,
)


@tagged('post_install', '-at_install')
class TestStaffAccess(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Wizard = cls.env['logistics.create.staff.user.wizard']
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Staff Access Seller',
            'zip': '682001',
            'email': 'staff.access.seller@example.com',
        })
        cls.wallet = cls.seller.wallet_ids[0]
        cls.pack_wallet = cls.env.ref('keralariders_logistics.staff_pack_wallet')
        cls.pack_sellers = cls.env.ref('keralariders_logistics.staff_pack_sellers')
        cls.pack_finance = cls.env.ref('keralariders_logistics.staff_pack_finance')
        cls.pack_admin = cls.env.ref('keralariders_logistics.staff_pack_administration')

    def _create_staff(self, login, packs, password='kx_staff_pass'):
        wizard = self.Wizard.create({
            'name': login,
            'login': login,
            'email': '%s@example.com' % login,
            'password': password,
            'send_invite': False,
            'pack_ids': [(6, 0, [pack.id for pack in packs])],
        })
        wizard.action_apply()
        user = self.env['res.users'].search([('login', '=', login)], limit=1)
        self.assertTrue(user, 'wizard should create the staff user')
        return user

    def test_wallet_only_cannot_search_sellers(self):
        staff = self._create_staff('kx_wallet_only', [self.pack_wallet])
        Seller = self.env['logistics.seller'].with_user(staff)
        with self.assertRaises(AccessError):
            Seller.search([])
        with self.assertRaises(AccessError):
            Seller.browse(self.seller.id).read(['name'])

        Wallet = self.env['logistics.wallet'].with_user(staff)
        wallets = Wallet.search([])
        self.assertIn(self.wallet, wallets)

        Account = self.env['logistics.account'].with_user(staff)
        with self.assertRaises(AccessError):
            Account.search([])

    def test_multi_select_assigns_multiple_groups(self):
        staff = self._create_staff(
            'kx_wallet_and_finance',
            [self.pack_wallet, self.pack_finance],
        )
        self.assertTrue(staff.has_group(WALLET_GROUP_XMLID))
        self.assertTrue(staff.has_group(
            'keralariders_logistics.group_staff_finance'))
        self.assertFalse(staff.has_group(
            'keralariders_logistics.group_staff_sellers'))
        self.assertFalse(staff.has_group(ADMIN_GROUP_XMLID))
        self.assertFalse(staff.has_group('base.group_portal'))
        self.assertTrue(staff.has_group('base.group_user'))
        self.assertTrue(staff.kx_is_office_staff)
        self.assertTrue(staff.has_group(
            'keralariders_logistics.group_staff_base'))

    def test_seller_pack_can_search_sellers(self):
        staff = self._create_staff('kx_seller_desk', [self.pack_sellers])
        sellers = self.env['logistics.seller'].with_user(staff).search([])
        self.assertIn(self.seller, sellers)
        visible = self.env['ir.ui.menu'].with_user(staff)._visible_menu_ids()
        self.assertIn(
            self.env.ref('keralariders_logistics.menu_logistics_seller_root').id,
            visible,
        )
        self.assertNotIn(
            self.env.ref('keralariders_logistics.menu_logistics_wallet_wallet').id,
            visible,
        )

    def test_admin_still_sees_all(self):
        self.assertTrue(self.env.user.has_group(ADMIN_GROUP_XMLID))
        self.assertIn(
            self.seller,
            self.env['logistics.seller'].search([]),
        )
        self.assertIn(
            self.wallet,
            self.env['logistics.wallet'].search([]),
        )
        visible = self.env['ir.ui.menu']._visible_menu_ids()
        self.assertIn(
            self.env.ref('keralariders_logistics.menu_logistics_seller_root').id,
            visible,
        )
        self.assertIn(
            self.env.ref('keralariders_logistics.menu_logistics_wallet_wallet').id,
            visible,
        )

    def test_wallet_only_hides_seller_and_finance_menus(self):
        staff = self._create_staff('kx_wallet_menus', [self.pack_wallet])
        visible = self.env['ir.ui.menu'].with_user(staff)._visible_menu_ids()
        self.assertIn(
            self.env.ref('keralariders_logistics.menu_logistics_wallet_wallet').id,
            visible,
        )
        self.assertIn(
            self.env.ref('keralariders_logistics.menu_logistics_wallet_recharge_request').id,
            visible,
        )
        self.assertNotIn(
            self.env.ref('keralariders_logistics.menu_logistics_seller_root').id,
            visible,
        )
        self.assertNotIn(
            self.env.ref('keralariders_logistics.menu_logistics_account_account').id,
            visible,
        )
        self.assertNotIn(
            self.env.ref('keralariders_logistics.menu_kx_create_staff_user').id,
            visible,
        )

    def test_administration_pack_grants_settings_not_portal(self):
        staff = self._create_staff('kx_office_admin', [self.pack_admin])
        self.assertTrue(staff.has_group(STAFF_ADMIN_GROUP_XMLID))
        self.assertTrue(staff.has_group('base.group_system'))
        self.assertFalse(staff.has_group('base.group_portal'))
        self.assertFalse(staff.has_group(ADMIN_GROUP_XMLID))
        with self.assertRaises(AccessError):
            self.env['logistics.seller'].with_user(staff).search([])

    def test_edit_staff_areas_later(self):
        staff = self._create_staff('kx_edit_areas', [self.pack_wallet])
        wizard = self.Wizard.create({
            'user_id': staff.id,
            'name': staff.name,
            'login': staff.login,
            'email': staff.email,
            'send_invite': False,
            'pack_ids': [(6, 0, [self.pack_wallet.id, self.pack_sellers.id])],
        })
        wizard.action_apply()
        staff.invalidate_recordset()
        self.assertTrue(staff.has_group(
            'keralariders_logistics.group_staff_sellers'))
        self.assertTrue(staff.has_group(WALLET_GROUP_XMLID))
        sellers = self.env['logistics.seller'].with_user(staff).search([])
        self.assertIn(self.seller, sellers)

    def test_finance_only_cannot_search_wallets(self):
        staff = self._create_staff('kx_finance_only', [self.pack_finance])
        with self.assertRaises(AccessError):
            self.env['logistics.wallet'].with_user(staff).search([])
        self.env['logistics.account'].with_user(staff).search([])

    def test_create_requires_a_pack(self):
        with self.assertRaises(UserError):
            self.Wizard.create({
                'name': 'No Packs',
                'login': 'kx_no_packs',
                'email': 'kx_no_packs@example.com',
                'password': 'kx_staff_pass',
                'send_invite': False,
            }).action_apply()

    def test_wallet_staff_can_approve_recharge(self):
        staff = self._create_staff('kx_wallet_approve', [self.pack_wallet])
        request = self.env['logistics.wallet.recharge.request'].create({
            'seller_id': self.seller.id,
            'wallet_id': self.wallet.id,
            'requested_amount': 50.0,
        })
        request.with_user(staff).action_approve_request()
        self.assertEqual(request.state, 'approved')
