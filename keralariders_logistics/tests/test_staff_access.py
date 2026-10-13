"""Office staff access packs: menus and ACLs follow the selected areas."""

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models.staff_access import (
    ADMIN_GROUP_XMLID,
    STAFF_ADMIN_GROUP_XMLID,
    WALLET_GROUP_XMLID,
)

FINANCE_MENU_XMLIDS = tuple('keralariders_logistics.%s' % name for name in (
    'menu_logistics_finance',
    'menu_logistics_wallet',
    'menu_logistics_wallet_wallet',
    'menu_wallet_transactions',
    'menu_logistics_wallet_recharge_request',
    'menu_logistics_account',
    'menu_logistics_account_account',
    'menu_logistics_account_transaction',
    'menu_logistics_account_transfers',
    'menu_logistics_account_transfer_cod_payment',
    'menu_logistics_account_transfer_cod_clearance',
    'menu_logistics_account_transfer_hub_deposit',
    'menu_logistics_account_transfer_hub_banking',
    'menu_logistics_account_transfer_other',
))

INDIAPOST_MENU_XMLIDS = tuple('keralariders_logistics.%s' % name for name in (
    'menu_indiapost_root',
    'menu_indiapost_barcode_range',
    'menu_indiapost_barcode',
    'menu_indiapost_office',
    'menu_indiapost_tariff_cache',
    'menu_indiapost_log',
    'menu_indiapost_scan_import',
))


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
        cls.pack_operations = cls.env.ref('keralariders_logistics.staff_pack_operations')
        cls.pack_operations = cls.env.ref('keralariders_logistics.staff_pack_operations')
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
        for xmlid in FINANCE_MENU_XMLIDS:
            self.assertNotIn(self.env.ref(xmlid).id, visible, xmlid)
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

    def test_finance_can_read_wallets_but_not_change_them(self):
        staff = self._create_staff('kx_finance_only', [self.pack_finance])
        wallets = self.env['logistics.wallet'].with_user(staff).search([])
        self.assertIn(self.wallet, wallets)
        with self.assertRaises(AccessError):
            self.env['logistics.wallet'].with_user(staff).create({
                'seller_id': self.seller.id,
            })
        self.env['logistics.account'].with_user(staff).search([])

    def test_seller_staff_reads_recharge_and_cannot_post_money(self):
        """Sellers + Operations staff can open a seller, including recharge rows.

        Same packs as an office login that is not Logistics Administrator and
        not Wallet staff. Read covers every seller. Create, write, unlink and
        the money buttons stay refused.
        """
        staff = self._create_staff(
            'kx_seller_ops_view',
            [self.pack_sellers, self.pack_operations],
        )
        self.assertFalse(staff.has_group(ADMIN_GROUP_XMLID))
        self.assertFalse(staff.has_group(WALLET_GROUP_XMLID))
        self.assertFalse(staff.has_group('base.group_portal'))

        other = self.env['logistics.seller'].create({
            'name': 'Other Staff Access Seller',
            'zip': '695001',
            'email': 'other.staff.access.seller@example.com',
        })
        request = self.env['logistics.wallet.recharge.request'].create({
            'seller_id': self.seller.id,
            'wallet_id': self.wallet.id,
            'requested_amount': 25.0,
        })
        other_request = self.env['logistics.wallet.recharge.request'].create({
            'seller_id': other.id,
            'wallet_id': other.wallet_ids[0].id,
            'requested_amount': 40.0,
        })
        txn = self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': 15.0,
            'reference': 'Staff read fixture',
        })

        rows = self.env['logistics.seller'].with_user(staff).browse(
            self.seller.id,
        ).read([
            'name', 'wallet_recharge_request_ids', 'wallet_recharge_request_count',
        ])
        self.assertEqual(rows[0]['name'], self.seller.name)
        self.assertIn(request.id, rows[0]['wallet_recharge_request_ids'])

        visible = self.env['logistics.wallet.recharge.request'].with_user(
            staff).search([])
        self.assertIn(request, visible)
        self.assertIn(other_request, visible)

        with self.assertRaises(AccessError):
            self.env['logistics.wallet.recharge.request'].with_user(staff).create({
                'seller_id': self.seller.id,
                'wallet_id': self.wallet.id,
                'requested_amount': 1.0,
            })
        with self.assertRaises(AccessError):
            request.with_user(staff).write({'remarks': 'staff edit'})
        with self.assertRaises(AccessError):
            request.with_user(staff).unlink()
        with self.assertRaises(AccessError):
            request.with_user(staff).action_approve_request()

        tx_as_staff = self.env['logistics.wallet.transaction'].with_user(staff)
        self.assertIn(txn, tx_as_staff.search([]))
        with self.assertRaises(AccessError):
            tx_as_staff.create({
                'wallet_id': self.wallet.id,
                'amount': 1.0,
            })
        with self.assertRaises(AccessError):
            txn.with_user(staff).write({'reference': 'staff edit'})
        with self.assertRaises(AccessError):
            txn.with_user(staff).unlink()

        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'Staff View Customer',
            'shipping_to_address': '1 Staff Road',
            'shipping_to_zip': '682001',
            'shipping_to_mobile': '9999999999',
            'item_description': 'View only',
            'total_weight': 0.5,
        })
        shipment.with_user(staff).read([
            'name', 'cod_payment_transfer_ids', 'wallet_transaction_id',
            'indiapost_barcode_id', 'seller_id',
        ])
        with self.assertRaises(AccessError) as caught:
            shipment.with_user(staff).with_context(
                kx_allow_wallet_debit=True,
            ).action_add_wallet_transaction()
        self.assertIn('wallet staff', str(caught.exception).lower())
        with self.assertRaises(AccessError):
            shipment.with_user(staff).action_add_cod_payment_transfer()

        with self.assertRaises(AccessError):
            self.seller.with_user(staff).write({'phone': '9000000000'})
        with self.assertRaises(AccessError):
            self.env['logistics.seller'].with_user(staff).create({
                'name': 'Staff Must Not Create',
                'zip': '682001',
                'email': 'staff.must.not.create@example.com',
            })
        with self.assertRaises(AccessError):
            self.seller.with_user(staff).unlink()

        order = self.env['logistics.order'].create({
            'seller_id': self.seller.id,
        })
        order.with_user(staff).read(['name', 'seller_id', 'shipment_ids'])
        with self.assertRaises(AccessError):
            order.with_user(staff).write({'pickup_date': '2026-01-02'})
        with self.assertRaises(AccessError):
            shipment.with_user(staff).write({'shipping_to_name': 'Changed'})
        with self.assertRaises(AccessError):
            shipment.with_user(staff).unlink()

    def _draft_shipment(self):
        return self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'Deduct Customer',
            'shipping_to_address': '1 Staff Road',
            'shipping_to_zip': '682001',
            'shipping_to_mobile': '9999999999',
            'item_description': 'Manual deduct',
            'total_weight': 0.5,
        })

    def test_manual_deduct_stays_with_wallet_staff_and_admin(self):
        """Deduct from Wallet is the button. Request Pickup is not this path."""
        self.seller.write({'fulfilment_method': 'own_network'})
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'Staff deduct top-up',
        })
        self.wallet.invalidate_recordset(['balance'])

        ops = self._create_staff('kx_ops_no_deduct', [self.pack_operations])
        self.assertFalse(ops.has_group(WALLET_GROUP_XMLID))
        self.assertFalse(ops.has_group(ADMIN_GROUP_XMLID))
        blocked = self._draft_shipment()
        opening = self.wallet.balance
        with self.assertRaises(AccessError) as caught:
            blocked.with_user(ops).action_add_wallet_transaction()
        self.assertIn('wallet staff', str(caught.exception).lower())
        self.assertFalse(blocked.wallet_transaction_id)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, opening, places=2)

        pack_network = self.env.ref('keralariders_logistics.staff_pack_network')
        wallet_staff = self._create_staff(
            'kx_wallet_can_deduct',
            [self.pack_wallet, self.pack_sellers, pack_network],
        )
        self.assertTrue(wallet_staff.has_group(WALLET_GROUP_XMLID))
        self.assertFalse(wallet_staff.has_group(ADMIN_GROUP_XMLID))
        by_wallet = self._draft_shipment()
        charge = by_wallet.delivery_charges_total
        self.assertGreater(charge, 0.0)
        by_wallet.with_user(wallet_staff).action_add_wallet_transaction()
        self.assertAlmostEqual(
            by_wallet.wallet_transaction_id.amount, -charge, places=2,
        )
        by_wallet.with_user(wallet_staff).action_add_wallet_transaction()
        self.assertEqual(
            self.env['logistics.wallet.transaction'].search_count([
                ('shipment_id', '=', by_wallet.id),
                ('amount', '<', 0),
            ]),
            1,
        )

        admin = self.env['res.users'].create({
            'name': 'KX Logistics Admin Deduct',
            'login': 'kx_logistics_admin_deduct',
            'email': 'kx_logistics_admin_deduct@example.com',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(ADMIN_GROUP_XMLID).id,
            ])],
        })
        self.assertFalse(admin.has_group(WALLET_GROUP_XMLID))
        by_admin = self._draft_shipment()
        admin_charge = by_admin.delivery_charges_total
        by_admin.with_user(admin).action_add_wallet_transaction()
        self.assertAlmostEqual(
            by_admin.wallet_transaction_id.amount, -admin_charge, places=2,
        )

    def test_create_requires_a_pack(self):
        with self.assertRaises(UserError):
            self.Wizard.create({
                'name': 'No Packs',
                'login': 'kx_no_packs',
                'email': 'kx_no_packs@example.com',
                'password': 'kx_staff_pass',
                'send_invite': False,
            }).action_apply()

    def _recharge_request(self, amount):
        return self.env['logistics.wallet.recharge.request'].create({
            'seller_id': self.seller.id,
            'wallet_id': self.wallet.id,
            'requested_amount': amount,
        })

    def test_no_staff_pack_can_approve_or_cancel_recharge(self):
        """Approve, cancel and reset belong to the Logistics Administrator.

        Wallet staff hold write on the request (the ACL is unchanged) and
        still cannot move it, so the refusal is the server guard, not the
        ACL or a hidden button.
        """
        cases = (
            ('kx_wallet_approve', [self.pack_wallet]),
            ('kx_finance_approve', [self.pack_finance]),
            ('kx_seller_ops_approve', [self.pack_sellers, self.pack_operations]),
            ('kx_wallet_finance_approve', [self.pack_wallet, self.pack_finance]),
        )
        for login, packs in cases:
            with self.subTest(login=login):
                staff = self._create_staff(login, packs)
                self.assertFalse(staff.has_group(ADMIN_GROUP_XMLID))
                request = self._recharge_request(50.0)
                with self.assertRaises(AccessError):
                    request.with_user(staff).action_approve_request()
                with self.assertRaises(AccessError):
                    request.with_user(staff).action_cancel()
                self.assertEqual(request.state, 'pending_approval')
                self.assertFalse(request.wallet_transaction_id)

                request.action_cancel()
                with self.assertRaises(AccessError):
                    request.with_user(staff).action_reset()
                self.assertEqual(request.state, 'cancelled')

        wallet_staff = self.env['res.users'].search(
            [('login', '=', 'kx_wallet_approve')])
        request = self._recharge_request(60.0)
        with self.assertRaises(AccessError):
            request.with_user(wallet_staff).write({
                'recharged_amount': 6000.0,
            })
        with self.assertRaises(AccessError):
            request.with_user(wallet_staff).write({'state': 'approved'})

    def test_admin_can_approve_recharge(self):
        admin = self.env['res.users'].create({
            'name': 'KX Logistics Admin',
            'login': 'kx_logistics_admin_approve',
            'email': 'kx_logistics_admin_approve@example.com',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(ADMIN_GROUP_XMLID).id,
            ])],
        })
        self.assertFalse(admin.has_group(WALLET_GROUP_XMLID))
        balance = self.wallet.balance
        request = self._recharge_request(75.0)
        request.with_user(admin).action_approve_request()
        self.assertEqual(request.state, 'approved')
        self.assertEqual(request.approved_by, admin)
        self.assertTrue(request.wallet_transaction_id)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, balance + 75.0)

        request.with_user(admin).action_cancel()
        self.assertEqual(request.state, 'cancelled')
        request.with_user(admin).action_reset()
        self.assertEqual(request.state, 'pending_approval')

    def _cod_fixture(self):
        Account = self.env['logistics.account']
        company = Account.create({
            'name': 'Staff Access Company COD', 'account_type': 'company',
        })
        seller_account = Account.create({
            'name': 'Staff Access Seller COD', 'account_type': 'seller',
            'seller_id': self.seller.id,
        })
        Account.create({
            'name': 'Staff Access COD Customer', 'account_type': 'cod_customer',
        })
        Account.create({'name': 'Staff Access Bank', 'account_type': 'bank'})
        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'COD Staff Customer',
            'shipping_to_address': '1 COD Road',
            'shipping_to_zip': '682001',
            'shipping_to_mobile': '9999999999',
            'item_description': 'COD lock',
            'total_weight': 0.5,
            'order_payment_type': 'cod',
            'total_order_value': 300.0,
            'cod_amount': 300.0,
        })
        return company, seller_account, shipment

    def _cod_withdrawal(self, company, seller_account, amount=100.0):
        return self.env['logistics.account.transfer'].create({
            'transfer_type': 'cod_withdrawal',
            'state': 'draft',
            'from_account_id': company.id,
            'to_account_id': seller_account.id,
            'amount': amount,
            'related_seller_id': self.seller.id,
        })

    def test_finance_staff_cannot_post_cod_money(self):
        """Approve, cancel, Mark Paid and Add Payment belong to the Administrator.

        Finance staff still read COD transfers so the forms open.
        """
        company, seller_account, shipment = self._cod_fixture()
        staff = self._create_staff('kx_finance_cod', [self.pack_finance])
        self.assertFalse(staff.has_group(ADMIN_GROUP_XMLID))

        draft = self._cod_withdrawal(company, seller_account)
        draft.with_user(staff).read(['name', 'state', 'amount'])
        with self.assertRaises(AccessError):
            draft.with_user(staff).action_approve()
        with self.assertRaises(AccessError):
            draft.with_user(staff).action_cancel_draft()
        self.assertEqual(draft.state, 'draft')

        draft.action_approve()
        with self.assertRaises(AccessError):
            draft.with_user(staff).action_mark_cod_paid()
        self.assertFalse(draft.cod_paid_on)

        with self.assertRaises(AccessError):
            shipment.with_user(staff).action_add_cod_payment_transfer()
        with self.assertRaises(AccessError):
            self.env['logistics.account.transfer'].with_user(staff).create({
                'transfer_type': 'cod_payment',
                'from_account_id': company.id,
                'to_account_id': seller_account.id,
                'amount': 10.0,
            })
        with self.assertRaises(AccessError):
            draft.with_user(staff).write({'cod_paid_on': '2026-10-12 10:00:00'})

        arch = self.env['logistics.account.transfer'].with_user(staff).get_views(
            [(False, 'form')])['views']['form']['arch']
        for method in ('action_approve', 'action_cancel_draft', 'action_mark_cod_paid'):
            self.assertNotIn('name="%s"' % method, arch, method)
        shipment_arch = self.env['logistics.shipment'].with_user(staff).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertNotIn('action_add_cod_payment_transfer', shipment_arch)

    def test_admin_can_post_cod_money(self):
        company, seller_account, shipment = self._cod_fixture()
        admin = self.env['res.users'].create({
            'name': 'KX COD Admin',
            'login': 'kx_logistics_admin_cod',
            'email': 'kx_logistics_admin_cod@example.com',
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref(ADMIN_GROUP_XMLID).id,
            ])],
        })
        self.assertFalse(admin.has_group(
            'keralariders_logistics.group_staff_finance'))

        approved = self._cod_withdrawal(company, seller_account)
        approved.with_user(admin).action_approve()
        self.assertEqual(approved.state, 'posted')
        approved.with_user(admin).action_mark_cod_paid()
        self.assertTrue(approved.cod_paid_on)
        self.assertEqual(approved.cod_paid_by, admin)

        cancelled = self._cod_withdrawal(company, seller_account, amount=50.0)
        cancelled.with_user(admin).action_cancel_draft()
        self.assertEqual(cancelled.state, 'cancelled')

        action = shipment.with_user(admin).action_add_cod_payment_transfer()
        self.assertEqual(action['res_model'], 'logistics.cod.payment.wizard')
        self.assertEqual(action['context']['default_shipment_id'], shipment.id)

        arch = self.env['logistics.account.transfer'].with_user(admin).get_views(
            [(False, 'form')])['views']['form']['arch']
        for method in ('action_approve', 'action_cancel_draft', 'action_mark_cod_paid'):
            self.assertIn('name="%s"' % method, arch, method)

    def test_finance_menu_is_administrator_only(self):
        admin_group = self.env.ref(ADMIN_GROUP_XMLID)
        for xmlid in FINANCE_MENU_XMLIDS:
            menu = self.env.ref(xmlid)
            self.assertEqual(menu.group_ids, admin_group, xmlid)

        all_packs = self.env['logistics.staff.access.pack'].search([])
        cases = [('kx_all_packs_menu', all_packs)] + [
            ('kx_menu_pack_%s' % pack.id, pack) for pack in all_packs
        ]
        for login, packs in cases:
            with self.subTest(login=login):
                staff = self._create_staff(login, packs)
                visible = self.env['ir.ui.menu'].with_user(staff)._visible_menu_ids()
                for xmlid in FINANCE_MENU_XMLIDS:
                    self.assertNotIn(self.env.ref(xmlid).id, visible, xmlid)

        visible = self.env['ir.ui.menu']._visible_menu_ids()
        for xmlid in FINANCE_MENU_XMLIDS:
            menu = self.env.ref(xmlid)
            if menu.active:
                self.assertIn(menu.id, visible, xmlid)

    def test_indiapost_menu_is_administrator_only(self):
        """India Post staff keep their ACLs; only the top menu is hidden."""
        admin_group = self.env.ref(ADMIN_GROUP_XMLID)
        for xmlid in INDIAPOST_MENU_XMLIDS:
            self.assertEqual(self.env.ref(xmlid).group_ids, admin_group, xmlid)

        all_packs = self.env['logistics.staff.access.pack'].search([])
        cases = [('kx_all_packs_ip_menu', all_packs)] + [
            ('kx_ip_menu_pack_%s' % pack.id, pack) for pack in all_packs
        ]
        for login, packs in cases:
            with self.subTest(login=login):
                staff = self._create_staff(login, packs)
                visible = self.env['ir.ui.menu'].with_user(staff)._visible_menu_ids()
                for xmlid in INDIAPOST_MENU_XMLIDS:
                    self.assertNotIn(self.env.ref(xmlid).id, visible, xmlid)

        visible = self.env['ir.ui.menu']._visible_menu_ids()
        for xmlid in INDIAPOST_MENU_XMLIDS:
            self.assertIn(self.env.ref(xmlid).id, visible, xmlid)

    def test_staff_reads_indiapost_shipment_without_menu(self):
        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'India Post Staff Customer',
            'shipping_to_address': '1 Post Road',
            'shipping_to_zip': '682001',
            'shipping_to_mobile': '9999999999',
            'item_description': 'India Post read',
            'total_weight': 0.5,
            'fulfilment_method': 'indiapost',
            'indiapost_article_number': 'EK123456789IN',
        })
        pack_indiapost = self.env.ref('keralariders_logistics.staff_pack_indiapost')
        root_menu = self.env.ref('keralariders_logistics.menu_indiapost_root')
        cases = (
            ('kx_ip_read_ops', [self.pack_operations]),
            ('kx_ip_read_indiapost', [pack_indiapost]),
            ('kx_ip_read_ops_indiapost', [self.pack_operations, pack_indiapost]),
        )
        for login, packs in cases:
            with self.subTest(login=login):
                staff = self._create_staff(login, packs)
                self.assertNotIn(
                    root_menu.id,
                    self.env['ir.ui.menu'].with_user(staff)._visible_menu_ids(),
                )
                row = shipment.with_user(staff).read([
                    'name', 'indiapost_article_number', 'indiapost_barcode_id',
                    'indiapost_booking_state', 'fulfilment_method',
                ])[0]
                self.assertEqual(row['indiapost_article_number'], 'EK123456789IN')
                arch = self.env['logistics.shipment'].with_user(staff).get_views(
                    [(False, 'form')])['views']['form']['arch']
                self.assertIn('indiapost_article_number', arch)

    def test_seller_form_hides_recharge_button_for_staff(self):
        staff = self._create_staff(
            'kx_seller_form_view',
            [self.pack_sellers, self.pack_operations],
        )
        Seller = self.env['logistics.seller']
        staff_arch = Seller.with_user(staff).get_views(
            [(False, 'form')])['views']['form']['arch']
        self.assertNotIn('action_view_recharge_requests', staff_arch)
        self.assertIn('action_view_wallets', staff_arch)
        self.seller.with_user(staff).web_read({
            'name': {}, 'wallet_count': {}, 'total_wallet_balance': {},
            'cod_pending_balance': {}, 'wallet_recharge_request_ids': {},
        })
        admin_arch = Seller.get_views([(False, 'form')])['views']['form']['arch']
        self.assertIn('action_view_recharge_requests', admin_arch)
