"""Hub-network COD: 1% settlement, credit on pickup, reverse on return.

India Post keeps the Settings withdrawal rate (default 2%) and is credited
only when the article is delivered. A KeralaXpress hub-network seller is
charged the hub-network rate (default 1%). Their COD is credited when the
parcel is picked up, and the delivery charge already taken from the wallet
is refunded. Both are reversed when the return is delivered back to the
seller. The later cash or UPI collection still records the executive's
custody, and does not credit the seller a second time.
"""

from datetime import date
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models.account import (
    COD_SETTLEMENT_CHARGE_PARAM,
    DEFAULT_COD_SETTLEMENT_CHARGE_PERCENT,
    DEFAULT_HUB_COD_SETTLEMENT_CHARGE_PERCENT,
    HUB_COD_SETTLEMENT_CHARGE_PARAM,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

OPS_PARAM = 'keralariders_logistics.ops_notification_email'


@tagged('post_install', '-at_install')
class TestHubNetworkCod(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=False)
        cls.Transfer = cls.env['logistics.account.transfer']
        cls.Account = cls.env['logistics.account']
        cls.ICP = cls.env['ir.config_parameter'].sudo()
        cls.company_account = cls.Account.create({
            'name': 'Hub COD Company Account',
            'account_type': 'company',
        })
        cls.ICP.set_param(
            'keralariders_logistics.company_cod_account_id',
            str(cls.company_account.id),
        )
        cls.hub_seller = cls.env['logistics.seller'].create({
            'name': 'Hub Network COD Seller',
            'email': 'hub.cod.seller@example.com',
            'zip': '682001',
            'bank_account_name': 'Hub Network COD Seller',
            'bank_account_number': '111122223333',
            'bank_ifsc': 'HDFC0001234',
            'bank_name': 'HDFC',
        })
        cls.hub_seller.with_context(
            allow_fulfilment_method_write=True,
        ).write({'fulfilment_method': 'own_network'})
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'India Post COD Rate Seller',
            'email': 'ip.cod.rate@example.com',
            'zip': '682001',
            'bank_account_name': 'India Post COD Rate Seller',
            'bank_account_number': '444455556666',
            'bank_ifsc': 'SBIN0004321',
            'bank_name': 'SBI',
        })
        cls.hub_wallet = cls.env['logistics.wallet'].create({
            'seller_id': cls.hub_seller.id,
        })
        cls.env['logistics.wallet.transaction'].create({
            'wallet_id': cls.hub_wallet.id,
            'amount': 500.0,
            'reference': 'Opening balance',
        })

    def setUp(self):
        super().setUp()
        self.ICP.set_param(COD_SETTLEMENT_CHARGE_PARAM, False)
        self.ICP.set_param(HUB_COD_SETTLEMENT_CHARGE_PARAM, False)
        self.ICP.set_param(OPS_PARAM, '')

    # ------------------------------------------------------------------
    # Fixtures
    # ------------------------------------------------------------------
    def _credit_cod(self, seller, amount):
        return self.Transfer.sudo().create({
            'transfer_type': 'cod_payment',
            'state': 'posted',
            'from_account_id': self.company_account.id,
            'to_account_id': self.company_account.id,
            'amount': amount,
            'related_seller_id': seller.id,
        })

    def _shipment(self, seller, payment='cod', cod=500.0, **overrides):
        vals = {
            'seller_id': seller.id,
            'shipping_to_name': 'COD Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Hub COD article',
            'total_weight': 0.4,
            'length_cm': 20,
            'breadth_cm': 15,
            'height_cm': 10,
            'order_payment_type': payment,
            'total_order_value': cod if payment == 'cod' else 100.0,
            'cod_amount': cod if payment == 'cod' else 0.0,
        }
        vals.update(overrides)
        shipment = self.env['logistics.shipment'].create(vals)
        # Drop any pincode-matched executive so marking picked does not
        # depend on that person's role in the test database.
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'pickup_requested',
            'pickup_executive_id': False,
            'delivery_executive_id': False,
            'custodian_de_id': False,
        })
        shipment.estimated_route_ids.write({'assigned_de_id': False})
        return shipment

    def _charge_wallet(self, shipment, amount):
        txn = self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.hub_wallet.id,
            'amount': -amount,
            'shipment_id': shipment.id,
            'reference': shipment.display_name,
            'description': 'Delivery charge',
        })
        shipment.with_context(allow_delivery_charge_write=True).write({
            'wallet_transaction_id': txn.id,
        })
        return txn

    def _transfers(self, shipment, transfer_type):
        return self.Transfer.search([
            ('shipment_id', '=', shipment.id),
            ('transfer_type', '=', transfer_type),
            ('state', '!=', 'cancelled'),
        ])

    def _wallet_line(self, shipment, reference):
        return self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', reference),
        ])

    def _collect(self, shipment, amount):
        cash = self.Account.create({
            'name': 'Customer Cash %s' % shipment.id,
            'account_type': 'cash',
        })
        de_account = self.Account.create({
            'name': 'DE Cash %s' % shipment.id,
            'account_type': 'cash',
        })
        wizard = self.env['logistics.cod.payment.wizard'].create({
            'shipment_id': shipment.id,
            'seller_id': shipment.seller_id.id,
            'amount': amount,
            'from_account_id': cash.id,
            'to_account_id': de_account.id,
        })
        wizard.action_create_transfer()
        payment = self._transfers(shipment, 'cod_payment')
        return payment, de_account

    # ------------------------------------------------------------------
    # Settlement rate
    # ------------------------------------------------------------------
    def test_hub_network_defaults_to_one_percent_and_india_post_to_two(self):
        self.assertEqual(DEFAULT_HUB_COD_SETTLEMENT_CHARGE_PERCENT, 1.0)
        self.assertEqual(DEFAULT_COD_SETTLEMENT_CHARGE_PERCENT, 2.0)
        self.assertEqual(self.Transfer._hub_cod_settlement_charge_percent(), 1.0)
        self.assertEqual(
            self.Transfer._cod_settlement_charge_percent(self.hub_seller), 1.0)
        self.assertEqual(
            self.Transfer._cod_settlement_charge_percent(self.ip_seller), 2.0)
        self.assertEqual(self.Transfer._cod_settlement_charge_percent(), 2.0)

        hub_preview = self.Transfer.get_cod_withdrawal_breakdown(
            1000.0, seller=self.hub_seller)
        ip_preview = self.Transfer.get_cod_withdrawal_breakdown(
            1000.0, seller=self.ip_seller)
        self.assertEqual(hub_preview['percent'], 1.0)
        self.assertAlmostEqual(hub_preview['charge'], 10.0, places=2)
        self.assertAlmostEqual(hub_preview['net'], 990.0, places=2)
        self.assertEqual(hub_preview['percent_label'], '1%')
        self.assertEqual(ip_preview['percent'], 2.0)
        self.assertAlmostEqual(ip_preview['charge'], 20.0, places=2)
        self.assertAlmostEqual(ip_preview['net'], 980.0, places=2)

    def test_settings_store_each_rate_and_a_request_keeps_its_own(self):
        self.env['res.config.settings'].create({
            'indiapost_enabled': False,
            'cod_settlement_charge_percent': 2.5,
            'hub_cod_settlement_charge_percent': 1.5,
        }).execute()
        self.assertEqual(
            self.Transfer._cod_settlement_charge_percent(self.hub_seller), 1.5)
        self.assertEqual(
            self.Transfer._cod_settlement_charge_percent(self.ip_seller), 2.5)

        self._credit_cod(self.hub_seller, 1000.0)
        with patch.object(
            type(self.Transfer), '_cod_withdrawal_request_date',
            return_value=date(2026, 9, 7),
        ):
            request = self.Transfer.action_create_cod_withdrawal(
                self.hub_seller, 1000.0)
        self.assertEqual(request.cod_charge_percent, 1.5)
        self.assertAlmostEqual(request.cod_net_amount, 985.0, places=2)

        self.env['res.config.settings'].create({
            'indiapost_enabled': False,
            'cod_settlement_charge_percent': 2.5,
            'hub_cod_settlement_charge_percent': 4.0,
        }).execute()
        request.invalidate_recordset()
        self.assertEqual(request.cod_charge_percent, 1.5)
        self.assertEqual(
            self.Transfer.get_cod_withdrawal_breakdown(
                1000.0, seller=self.hub_seller)['percent'],
            4.0,
        )

    def test_hub_rate_of_zero_is_stored(self):
        self.env['res.config.settings'].create({
            'indiapost_enabled': False,
            'cod_settlement_charge_percent': 2.0,
            'hub_cod_settlement_charge_percent': 0.0,
        }).execute()
        self.assertEqual(self.Transfer._hub_cod_settlement_charge_percent(), 0.0)
        self.assertEqual(
            self.env['res.config.settings'].create({}).hub_cod_settlement_charge_percent,
            0.0,
        )
        self.assertEqual(
            self.Transfer._cod_settlement_charge_percent(self.ip_seller), 2.0)

    def test_settings_refuse_an_out_of_range_hub_rate(self):
        with self.assertRaises(ValidationError):
            self.env['res.config.settings'].create({
                'indiapost_enabled': False,
                'cod_settlement_charge_percent': 2.0,
                'hub_cod_settlement_charge_percent': 100.0,
            }).execute()

    # ------------------------------------------------------------------
    # Pickup
    # ------------------------------------------------------------------
    def test_pickup_credits_cod_and_refunds_the_delivery_charge_once(self):
        shipment = self._shipment(self.hub_seller, cod=500.0)
        self._charge_wallet(shipment, 48.0)
        self.assertAlmostEqual(self.hub_wallet.balance, 452.0, places=2)

        shipment.action_mark_picked()

        pickup = self._transfers(shipment, 'cod_pickup')
        self.assertEqual(len(pickup), 1)
        self.assertEqual(pickup.amount, 500.0)
        self.assertEqual(pickup.state, 'posted')
        self.assertEqual(pickup.related_seller_id, self.hub_seller)
        self.assertFalse(shipment.cod_payment_transfer_ids)
        self.assertAlmostEqual(shipment.cod_balance_amount, 500.0, places=2)
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            500.0, places=2)
        refund = self._wallet_line(shipment, 'KX-DLV-REFUND:%s' % shipment.id)
        self.assertEqual(len(refund), 1)
        self.assertAlmostEqual(refund.amount, 48.0, places=2)
        self.assertAlmostEqual(self.hub_wallet.balance, 500.0, places=2)

        shipment._kx_settle_own_network_cod_on_pickup()
        self.assertEqual(len(self._transfers(shipment, 'cod_pickup')), 1)
        self.assertEqual(len(self._wallet_line(
            shipment, 'KX-DLV-REFUND:%s' % shipment.id)), 1)
        self.assertAlmostEqual(self.hub_wallet.balance, 500.0, places=2)
        with self.assertRaises(UserError):
            shipment.action_mark_picked()

    def test_pickup_refunds_nothing_when_the_wallet_was_not_charged(self):
        shipment = self._shipment(self.hub_seller, cod=200.0)
        shipment.action_mark_picked()
        self.assertEqual(len(self._transfers(shipment, 'cod_pickup')), 1)
        self.assertFalse(self._wallet_line(
            shipment, 'KX-DLV-REFUND:%s' % shipment.id))
        self.assertAlmostEqual(self.hub_wallet.balance, 500.0, places=2)

        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'out_for_delivery',
            'is_return_journey': True,
        })
        shipment.action_mark_delivered()
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            0.0, places=2)
        self.assertFalse(self._wallet_line(
            shipment, 'KX-DLV-RESTORE:%s' % shipment.id))
        self.assertAlmostEqual(self.hub_wallet.balance, 500.0, places=2)

    def test_prepaid_and_india_post_pickups_do_not_credit(self):
        prepaid = self._shipment(self.hub_seller, payment='prepaid')
        self._charge_wallet(prepaid, 30.0)
        balance = self.hub_wallet.balance
        prepaid.action_mark_picked()
        self.assertFalse(self._transfers(prepaid, 'cod_pickup'))
        self.assertFalse(self._wallet_line(
            prepaid, 'KX-DLV-REFUND:%s' % prepaid.id))
        self.assertAlmostEqual(self.hub_wallet.balance, balance, places=2)

        ip = self._shipment(self.ip_seller, cod=180.0)
        ip.action_mark_picked()
        self.assertFalse(self._transfers(ip, 'cod_pickup'))
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.ip_seller),
            0.0, places=2)

    def test_return_pickup_does_not_credit(self):
        shipment = self._shipment(self.hub_seller, cod=150.0)
        self._charge_wallet(shipment, 20.0)
        balance = self.hub_wallet.balance
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'return_requested',
            'is_return_journey': True,
        })
        shipment.action_mark_picked()
        self.assertEqual(shipment.state, 'return_picked')
        self.assertFalse(self._transfers(shipment, 'cod_pickup'))
        self.assertFalse(self._wallet_line(
            shipment, 'KX-DLV-REFUND:%s' % shipment.id))
        self.assertAlmostEqual(self.hub_wallet.balance, balance, places=2)

    # ------------------------------------------------------------------
    # Customer collection
    # ------------------------------------------------------------------
    def test_collection_after_pickup_does_not_credit_the_seller_again(self):
        shipment = self._shipment(self.hub_seller, cod=500.0)
        shipment.action_mark_picked()
        payment, de_account = self._collect(shipment, 500.0)

        self.assertEqual(len(payment), 1)
        self.assertEqual(payment.transfer_type, 'cod_payment')
        self.assertFalse(payment.related_seller_id)
        self.assertEqual(payment.to_account_id, de_account)
        self.assertIn(payment, shipment.cod_payment_transfer_ids)
        self.assertAlmostEqual(shipment.cod_balance_amount, 0.0, places=2)
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            500.0, places=2)
        undeposited = self.Transfer.search([
            ('transfer_type', '=', 'cod_payment'),
            ('to_account_id', '=', de_account.id),
            ('hub_deposit_transfer_id', '=', False),
        ])
        self.assertIn(payment, undeposited)

    def test_collection_without_a_pickup_advance_still_credits_the_seller(self):
        shipment = self._shipment(self.hub_seller, cod=220.0)
        payment, _de = self._collect(shipment, 220.0)
        self.assertEqual(payment.related_seller_id, self.hub_seller)
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            220.0, places=2)

    # ------------------------------------------------------------------
    # Return delivered back to the seller
    # ------------------------------------------------------------------
    def test_return_delivered_reverses_cod_and_the_delivery_charge_once(self):
        shipment = self._shipment(self.hub_seller, cod=500.0)
        self._charge_wallet(shipment, 48.0)
        shipment.action_mark_picked()
        self.assertAlmostEqual(self.hub_wallet.balance, 500.0, places=2)

        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'out_for_delivery',
        })
        shipment.action_mark_delivered()
        self.assertEqual(shipment.state, 'delivered')
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            500.0, places=2)
        self.assertFalse(self._wallet_line(
            shipment, 'KX-DLV-RESTORE:%s' % shipment.id))

        shipment.action_request_return()
        self.assertEqual(shipment.state, 'return_requested')
        self.assertTrue(shipment.is_return_journey)
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            500.0, places=2)
        self.assertAlmostEqual(self.hub_wallet.balance, 500.0, places=2)

        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'out_for_delivery',
        })
        shipment.action_mark_delivered()
        self.assertEqual(shipment.state, 'returned')
        reversal = self._transfers(shipment, 'cod_return')
        self.assertEqual(len(reversal), 1)
        self.assertEqual(reversal.amount, 500.0)
        self.assertEqual(reversal.related_seller_id, self.hub_seller)
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            0.0, places=2)
        restore = self._wallet_line(
            shipment, 'KX-DLV-RESTORE:%s' % shipment.id)
        self.assertEqual(len(restore), 1)
        self.assertAlmostEqual(restore.amount, -48.0, places=2)
        self.assertAlmostEqual(self.hub_wallet.balance, 452.0, places=2)

        shipment._kx_reverse_own_network_cod_on_return()
        self.assertEqual(len(self._transfers(shipment, 'cod_return')), 1)
        self.assertEqual(len(self._wallet_line(
            shipment, 'KX-DLV-RESTORE:%s' % shipment.id)), 1)
        self.assertAlmostEqual(self.hub_wallet.balance, 452.0, places=2)

    def test_return_after_withdrawal_leaves_a_negative_pending_balance(self):
        shipment = self._shipment(self.hub_seller, cod=500.0)
        self._charge_wallet(shipment, 40.0)
        shipment.action_mark_picked()
        self.assertAlmostEqual(self.hub_wallet.balance, 500.0, places=2)

        with patch.object(
            type(self.Transfer), '_cod_withdrawal_request_date',
            return_value=date(2026, 9, 8),
        ):
            withdrawal = self.Transfer.action_create_cod_withdrawal(
                self.hub_seller, 500.0)
        self.assertEqual(withdrawal.cod_charge_percent, 1.0)
        withdrawal.action_approve()
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            0.0, places=2)
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_withdrawable_balance(self.hub_seller),
            0.0, places=2)

        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.hub_wallet.id,
            'amount': -self.hub_wallet.balance,
            'reference': 'Spent the refund',
        })
        self.assertAlmostEqual(self.hub_wallet.balance, 0.0, places=2)

        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'out_for_delivery',
            'is_return_journey': True,
        })
        shipment.action_mark_delivered()

        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            -500.0, places=2)
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_withdrawable_balance(self.hub_seller),
            0.0, places=2)
        self.assertAlmostEqual(self.hub_wallet.balance, -40.0, places=2)
        self.hub_seller.invalidate_recordset(['cod_pending_balance'])
        self.assertAlmostEqual(self.hub_seller.cod_pending_balance, -500.0, places=2)

    def test_india_post_return_leaves_the_delivery_credit_in_place(self):
        shipment = self._shipment(self.ip_seller, cod=180.0)
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'out_for_delivery',
        })
        shipment.action_mark_delivered()
        self.assertEqual(shipment.state, 'delivered')
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.ip_seller),
            180.0, places=2)

        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'out_for_delivery',
            'is_return_journey': True,
        })
        shipment.action_mark_delivered()
        self.assertEqual(shipment.state, 'returned')
        self.assertFalse(self._transfers(shipment, 'cod_pickup'))
        self.assertFalse(self._transfers(shipment, 'cod_return'))
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.ip_seller),
            180.0, places=2)

    def test_return_without_a_pickup_advance_moves_nothing(self):
        shipment = self._shipment(self.hub_seller, cod=90.0)
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'out_for_delivery',
            'is_return_journey': True,
        })
        shipment.action_mark_delivered()
        self.assertEqual(shipment.state, 'returned')
        self.assertFalse(self._transfers(shipment, 'cod_return'))
        self.assertFalse(self._wallet_line(
            shipment, 'KX-DLV-RESTORE:%s' % shipment.id))
        self.assertAlmostEqual(
            self.Transfer.get_seller_cod_pending_balance(self.hub_seller),
            0.0, places=2)
