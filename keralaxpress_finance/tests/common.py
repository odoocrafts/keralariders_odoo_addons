from datetime import date

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

OCT_1 = date(2026, 10, 1)
OCT_31 = date(2026, 10, 31)


class KxFinanceFixtures(IndiapostHermeticMixin):
    """Two own-network sellers with known October wallet and COD activity.

    Seller A (Kerala, same state as the company):
      wallet  +1000 recharge, -100 and -150 bookings, +150 cancel refund,
              -20 rate adjustment, -5 manual debit
      COD     +500 collected on 5 Oct, 400 withdrawn on 10 Oct at 2%
    Seller B (Tamil Nadu): one -100 booking, nothing else.
    """

    @classmethod
    def _kx_setup_fixtures(cls):
        cls._ip_make_hermetic(enabled=False)
        cls.kerala = cls.env.ref('base.state_in_kl')
        cls.tamil_nadu = cls.env.ref('base.state_in_tn')
        cls.env.company.write({'state_id': cls.kerala.id, 'vat': '32AAAAA0000A1Z5'})
        cls.env['ir.config_parameter'].sudo().set_param(
            'keralaxpress_finance.invoice_gst_percent', '18')

        cls.Account = cls.env['logistics.account']
        cls.Transfer = cls.env['logistics.account.transfer']
        cls.company_account = cls.Account.create({
            'name': 'Finance Test Company Account', 'account_type': 'company'})
        cls.env['ir.config_parameter'].sudo().set_param(
            'keralariders_logistics.company_cod_account_id', str(cls.company_account.id))

        cls.seller_a = cls._kx_seller('Finance Seller A', cls.kerala, '32BBBBB1111B1Z5')
        cls.seller_b = cls._kx_seller('Finance Seller B', cls.tamil_nadu, '33CCCCC2222C1Z5')
        cls.wallet_a = cls.seller_a.wallet_ids[:1]
        cls.wallet_b = cls.seller_b.wallet_ids[:1]

        cls.s1 = cls._kx_shipment(cls.seller_a, 'delivered', 1000.0, date(2026, 10, 3))
        cls.s2 = cls._kx_shipment(cls.seller_a, 'cancelled', 500.0, date(2026, 10, 4))
        cls.s3 = cls._kx_shipment(cls.seller_a, 'pickup_requested', 200.0, date(2026, 10, 6))
        cls.s4 = cls._kx_shipment(cls.seller_b, 'delivered', 300.0, date(2026, 10, 7))

        request = cls.env['logistics.wallet.recharge.request'].create({
            'seller_id': cls.seller_a.id,
            'wallet_id': cls.wallet_a.id,
            'requested_amount': 1000.0,
        })
        request.action_approve_request()
        request.wallet_transaction_id.transaction_date = date(2026, 10, 1)
        cls.recharge_line = request.wallet_transaction_id

        cls._kx_wallet(cls.wallet_a, -100.0, date(2026, 10, 3), cls.s1.display_name, cls.s1)
        cls._kx_wallet(cls.wallet_a, -150.0, date(2026, 10, 4), cls.s2.display_name, cls.s2)
        cls._kx_wallet(cls.wallet_a, 150.0, date(2026, 10, 4), 'IP-CANCEL:%s' % cls.s2.id, cls.s2)
        cls._kx_wallet(cls.wallet_a, -20.0, date(2026, 10, 8), 'IP-SCAN-ADJ:%s' % cls.s1.id, cls.s1)
        cls._kx_wallet(cls.wallet_a, -5.0, date(2026, 10, 9), 'Manual adjustment')
        cls._kx_wallet(cls.wallet_b, 500.0, date(2026, 10, 1), 'Recharge - opening')
        cls._kx_wallet(cls.wallet_b, -100.0, date(2026, 10, 7), cls.s4.display_name, cls.s4)

        cls.cod_payment = cls.Transfer.sudo().create({
            'transfer_type': 'cod_payment',
            'state': 'posted',
            'from_account_id': cls.company_account.id,
            'to_account_id': cls.company_account.id,
            'amount': 500.0,
            'transfer_date': date(2026, 10, 5),
            'related_seller_id': cls.seller_a.id,
        })
        cls.cod_withdrawal = cls.Transfer.sudo().create({
            'transfer_type': 'cod_withdrawal',
            'state': 'posted',
            'from_account_id': cls.company_account.id,
            'to_account_id': cls.seller_a.seller_account_id.id,
            'amount': 400.0,
            'cod_charge_percent': 2.0,
            'transfer_date': date(2026, 10, 10),
            'related_seller_id': cls.seller_a.id,
        })
        cls.env.flush_all()

    @classmethod
    def _kx_seller(cls, name, state, gstin):
        seller = cls.env['logistics.seller'].create({
            'name': name,
            'email': '%s@example.com' % name.lower().replace(' ', '.'),
            'zip': '682001',
        })
        seller.with_context(allow_fulfilment_method_write=True).write(
            {'fulfilment_method': 'own_network'})
        seller.partner_id.write({'state_id': state.id, 'vat': gstin})
        return seller

    @classmethod
    def _kx_shipment(cls, seller, state, value, order_date):
        shipment = cls.env['logistics.shipment'].create({
            'seller_id': seller.id,
            'shipping_to_name': 'Finance Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Finance test article',
            'total_weight': 0.5,
            'length_cm': 20,
            'breadth_cm': 15,
            'height_cm': 10,
            'order_payment_type': 'prepaid',
            'total_order_value': value,
            'order_date': order_date,
        })
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': state,
            'pickup_executive_id': False,
            'delivery_executive_id': False,
        })
        return shipment

    @classmethod
    def _kx_wallet(cls, wallet, amount, when, reference, shipment=None):
        return cls.env['logistics.wallet.transaction'].create({
            'wallet_id': wallet.id,
            'amount': amount,
            'transaction_date': when,
            'reference': reference,
            'shipment_id': shipment.id if shipment else False,
        })
