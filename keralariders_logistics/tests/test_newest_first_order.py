"""Orders, shipments, COD transfers and wallet recharge requests list newest first.

``create_date desc, id desc`` is the model ``_order`` and the list/kanban
``default_order``. A later business date does not outrank a newer record.
The seller portal wallet page shows only the first five recharge requests.
"""

from datetime import date, datetime

from odoo.tests import HttpCase, TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestNewestFirstOrder(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Newest First Seller',
            'zip': '682001',
        })
        cls.wallet = cls.seller.wallet_ids[0]
        Account = cls.env['logistics.account']
        cls.from_account = Account.create({
            'name': 'Newest First Cash',
            'account_type': 'cash',
        })
        cls.to_account = Account.create({
            'name': 'Newest First Bank',
            'account_type': 'bank',
        })

    def _order(self, **vals):
        return self.env['logistics.order'].create(
            dict({'seller_id': self.seller.id}, **vals))

    def _shipment(self, order, **vals):
        return self.env['logistics.shipment'].create(dict({
            'order_id': order.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'Newest First Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.0,
        }, **vals))

    def _transfer(self, amount):
        return self.env['logistics.account.transfer'].create({
            'transfer_type': 'other',
            'state': 'posted',
            'from_account_id': self.from_account.id,
            'to_account_id': self.to_account.id,
            'amount': amount,
            'transfer_date': date(2026, 10, 1),
        })

    def _recharge(self, amount, request_date):
        return self.env['logistics.wallet.recharge.request'].create({
            'seller_id': self.seller.id,
            'wallet_id': self.wallet.id,
            'requested_amount': amount,
            'request_date': request_date,
        })

    def test_orders_newest_first(self):
        Order = self.env['logistics.order']
        first = self._order()
        second = self._order()
        backdated = self._order(order_date=date(2020, 1, 1))
        found = Order.search([('id', 'in', (first | second | backdated).ids)])
        self.assertEqual(found.ids, [backdated.id, second.id, first.id])

    def test_shipments_newest_first(self):
        order = self._order()
        first = self._shipment(order)
        second = self._shipment(order)
        found = self.env['logistics.shipment'].search(
            [('id', 'in', (first | second).ids)])
        self.assertEqual(found.ids, [second.id, first.id])
        order.invalidate_recordset(['shipment_ids'])
        self.assertEqual(order.shipment_ids.ids, [second.id, first.id])

    def test_cod_transfers_newest_first(self):
        first = self._transfer(10.0)
        second = self._transfer(20.0)
        found = self.env['logistics.account.transfer'].search(
            [('id', 'in', (first | second).ids)])
        self.assertEqual(found.ids, [second.id, first.id])

    def test_recharge_requests_newest_first(self):
        same_moment = datetime(2026, 10, 1, 9, 0, 0)
        first = self._recharge(100.0, same_moment)
        second = self._recharge(200.0, same_moment)
        found = self.env['logistics.wallet.recharge.request'].search(
            [('id', 'in', (first | second).ids)])
        self.assertEqual(found.ids, [second.id, first.id])


@tagged('post_install', '-at_install')
class TestNewestFirstPortalWallet(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Newest First Portal Seller',
            'zip': '682001',
        })
        cls.wallet = cls.seller.wallet_ids[0]
        cls.login = 'kx_newest_first_portal'
        cls.env['res.users'].create({
            'name': 'Newest First Portal',
            'login': cls.login,
            'password': cls.login,
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def test_wallet_page_lists_newest_recharge_first(self):
        same_moment = datetime(2026, 10, 1, 9, 0, 0)
        Request = self.env['logistics.wallet.recharge.request']
        requests = Request.browse()
        for amount in (10.0, 20.0, 30.0, 40.0, 50.0, 60.0):
            requests |= Request.create({
                'seller_id': self.seller.id,
                'wallet_id': self.wallet.id,
                'requested_amount': amount,
                'request_date': same_moment,
            })
        oldest, newest = requests[0], requests[-1]

        self.authenticate(self.login, self.login)
        page = self.url_open('/my/wallet')
        self.assertEqual(page.status_code, 200)
        self.assertIn(newest.name, page.text)
        self.assertNotIn(oldest.name, page.text)
        self.assertLess(
            page.text.index(newest.name), page.text.index(requests[-2].name))
