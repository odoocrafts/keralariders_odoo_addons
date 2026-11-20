"""Hub-network task split, pickup scan sheet, riders, and the COD window."""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


@tagged('post_install', '-at_install')
class TestKxNetworkOps(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=False)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Network Ops Seller',
            'email': 'network.ops.seller@example.com',
            'zip': '682001',
            'bank_account_name': 'Network Ops Seller',
            'bank_account_number': '999988887777',
            'bank_ifsc': 'HDFC0001234',
            'bank_name': 'HDFC',
        })
        cls.seller.with_context(allow_fulfilment_method_write=True).write({
            'fulfilment_method': 'own_network',
        })
        cls.executive = cls.env['logistics.delivery.executive'].create({
            'name': 'Network Ops Rider',
            'mobile': '9000001111',
            'is_pickup': True,
            'is_delivery': True,
            'is_driver': True,
        })
        cls.company_account = cls.env['logistics.account'].create({
            'name': 'Network Ops Company COD',
            'account_type': 'company',
        })
        cls.env['ir.config_parameter'].sudo().set_param(
            'keralariders_logistics.company_cod_account_id',
            str(cls.company_account.id),
        )
        cls.wallet = cls.seller.wallet_ids[:1]
        cls.env['logistics.wallet.transaction'].create({
            'wallet_id': cls.wallet.id,
            'amount': 500.0,
            'reference': 'Opening balance',
        })

    def _pin(self, district_name):
        self.env.cr.execute(
            "SELECT name FROM logistics_pincode WHERE district_name ILIKE %s LIMIT 1",
            (district_name,),
        )
        row = self.env.cr.fetchone()
        self.assertTrue(row, 'no pincode seeded for %s' % district_name)
        return row[0]

    def _shipment(self, **overrides):
        vals = {
            'seller_id': self.seller.id,
            'shipping_to_name': 'Ops Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Ops article',
            'total_weight': 0.4,
            'order_payment_type': 'cod',
            'total_order_value': 100.0,
            'cod_amount': 100.0,
        }
        vals.update(overrides)
        shipment = self.env['logistics.shipment'].create(vals)
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'pickup_requested',
            'pickup_executive_id': self.executive.id,
            'delivery_executive_id': False,
            'custodian_de_id': False,
        })
        return shipment

    def test_a_delivery_rider_can_take_a_hub_transfer(self):
        Shipment = self.env['logistics.shipment']
        delivery = self.env['logistics.delivery.executive'].create({
            'name': 'Delivery Rider',
            'mobile': '9000005555',
            'is_pickup': True,
            'is_delivery': True,
            'is_driver': False,
        })
        pickup_only = self.env['logistics.delivery.executive'].create({
            'name': 'Pickup Only Rider',
            'mobile': '9000006666',
            'is_pickup': True,
            'is_delivery': False,
            'is_driver': False,
        })
        self.assertTrue(Shipment._de_eligible_for_operation(delivery, 'hub_transfer'))
        self.assertTrue(Shipment._de_eligible_for_operation(self.executive, 'hub_transfer'))
        self.assertFalse(Shipment._de_eligible_for_operation(pickup_only, 'hub_transfer'))

    def test_a_pickup_scan_does_not_move_money(self):
        shipment = self._shipment()
        self.assertFalse(shipment._kx_validate_pickup_scan(self.executive))
        self.assertFalse(self.env['logistics.account.transfer'].search([
            ('shipment_id', '=', shipment.id),
            ('transfer_type', '=', 'cod_pickup'),
        ]))
        other = self.env['logistics.delivery.executive'].create({
            'name': 'Other Rider',
            'mobile': '9000002222',
            'is_pickup': True,
        })
        self.assertTrue(shipment._kx_validate_pickup_scan(other))
        ip = self._shipment()
        self.env.cr.execute(
            "UPDATE logistics_shipment SET fulfilment_method = 'indiapost' WHERE id = %s",
            [ip.id],
        )
        ip.invalidate_recordset(['fulfilment_method'])
        reason = ip._kx_validate_pickup_scan(self.executive)
        self.assertIn('India Post', reason)

    def test_pickup_leaves_my_tasks_once_the_hub_has_custody(self):
        shipment = self._shipment(
            shipping_to_zip=self._pin('kozhikode'),
        )
        shipment.write({'shipping_from_zip': self._pin('malappuram')})
        Shipment = self.env['logistics.shipment']
        self.assertIn(shipment, Shipment.search(self.executive._pickup_tasks_domain()))
        self.assertNotIn(shipment, Shipment.search(self.executive._delivery_tasks_domain()))
        shipment.action_mark_picked()
        self.assertEqual(shipment.custodian_type, 'de')
        self.assertIn(shipment, Shipment.search(self.executive._pickup_tasks_domain()))
        shipment.action_hub_receive(hub=shipment.get_preferred_portal_drop_hub())
        self.assertEqual(shipment.custodian_type, 'hub')
        self.assertNotIn(shipment, Shipment.search(self.executive._pickup_tasks_domain()))
        driver = self.env['logistics.delivery.executive'].create({
            'name': 'Hub Transfer Driver',
            'mobile': '9000003333',
            'is_driver': True,
            'is_delivery': False,
            'is_pickup': False,
        })
        leg = shipment.estimated_route_ids.filtered(
            lambda route: route.operation_type == 'hub_transfer' and route.state != 'done'
        )[:1]
        self.assertTrue(leg)
        leg.write({'assigned_de_id': driver.id, 'state': 'assigned'})
        shipment.with_context(allow_shipment_state_write=True).write({
            'active_leg_id': leg.id,
        })
        self.assertIn(shipment, Shipment.search(driver._delivery_tasks_domain()))
        self.assertNotIn(shipment, Shipment.search(driver._pickup_tasks_domain()))

    def test_hub_rider_rows_are_own_network_custody_and_cash(self):
        koz_pin = self._pin('kozhikode')
        pin = self.env['logistics.pincode'].search([('name', '=', koz_pin)], limit=1)
        self.executive.assigned_pickup_pincodes = [(4, pin.id)]
        shipment = self._shipment(shipping_to_zip=koz_pin)
        shipment.write({'shipping_from_zip': self._pin('malappuram')})
        shipment.action_mark_picked()
        self.env['logistics.account.transaction'].create({
            'account_id': self.executive.default_cash_account_id.id,
            'amount': 125.0,
        })
        self.executive.default_cash_account_id.invalidate_recordset(['balance'])
        koz = self.env.ref('keralariders_logistics.hub_kozhikode')
        rows = [
            row for row in koz._kx_network_rider_rows()
            if row['executive'] == self.executive
        ]
        self.assertEqual(len(rows), 1)
        self.assertIn(shipment, rows[0]['shipments'])
        self.assertAlmostEqual(rows[0]['cash_balance'], 125.0, places=2)
        ip = self._shipment(shipping_to_zip=koz_pin)
        self.env.cr.execute(
            "UPDATE logistics_shipment SET fulfilment_method = 'indiapost', "
            "custodian_type = 'de', custodian_de_id = %s WHERE id = %s",
            [self.executive.id, ip.id],
        )
        ip.invalidate_recordset(['fulfilment_method', 'custodian_type', 'custodian_de_id'])
        again = [
            row for row in koz._kx_network_rider_rows()
            if row['executive'] == self.executive
        ]
        self.assertNotIn(ip, again[0]['shipments'])

    def test_own_network_sellers_have_no_cod_cycle_window(self):
        Transfer = self.env['logistics.account.transfer']
        info, blocked = Transfer.portal_cod_cycle_display(self.seller)
        self.assertFalse(info)
        self.assertFalse(blocked)
        ip_seller = self.env['logistics.seller'].create({
            'name': 'Cycle India Post Seller',
            'email': 'cycle.ip.seller@example.com',
            'zip': '682001',
        })
        ip_info, ip_blocked = Transfer.portal_cod_cycle_display(ip_seller)
        self.assertTrue(ip_info)
        self.assertIn('summary', ip_info)
        self.assertFalse(ip_blocked)

    def _charge_wallet(self, shipment, amount):
        txn = self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': -amount,
            'shipment_id': shipment.id,
            'reference': shipment.display_name,
        })
        shipment.with_context(allow_delivery_charge_write=True).write({
            'wallet_transaction_id': txn.id,
        })
        return txn

    def _transfers(self, shipment, transfer_type):
        return self.env['logistics.account.transfer'].search([
            ('shipment_id', '=', shipment.id),
            ('transfer_type', '=', transfer_type),
            ('state', '!=', 'cancelled'),
        ])

    def test_cancel_after_pickup_reverses_the_cod_advance(self):
        shipment = self._shipment(cod_amount=500.0, total_order_value=500.0)
        self._charge_wallet(shipment, 48.0)
        shipment.action_mark_picked()
        Transfer = self.env['logistics.account.transfer']
        self.assertAlmostEqual(
            Transfer.get_seller_cod_pending_balance(self.seller), 500.0, places=2)
        shipment.action_cancel_shipment()
        self.assertEqual(shipment.state, 'cancelled')
        self.assertEqual(len(self._transfers(shipment, 'cod_return')), 1)
        self.assertAlmostEqual(
            Transfer.get_seller_cod_pending_balance(self.seller), 0.0, places=2)
        self.assertFalse(self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', 'KX-DLV-REFUND:%s' % shipment.id),
        ]))
        refund = self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', 'KX-CANCEL-CHARGE:%s' % shipment.id),
        ])
        self.assertEqual(len(refund), 1)
        self.assertAlmostEqual(refund.amount, 48.0, places=2)
        self.assertAlmostEqual(self.wallet.balance, 500.0, places=2)
        self.assertTrue(shipment.wallet_transaction_id)
        shipment.action_cancel_shipment()
        self.assertEqual(len(self._transfers(shipment, 'cod_return')), 1)
        self.assertEqual(self.env['logistics.wallet.transaction'].search_count([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', 'KX-CANCEL-CHARGE:%s' % shipment.id),
        ]), 1)

    def test_customer_cancel_on_delivery_failure_reverses_cod_and_shipping(self):
        shipment = self._shipment(cod_amount=320.0, total_order_value=320.0)
        self._charge_wallet(shipment, 48.0)
        shipment.action_mark_picked()
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'out_for_delivery',
        })
        shipment.action_mark_delivery_failed(
            delivery_remarks='Customer cancelled the order. No cash.')
        self.assertEqual(shipment.state, 'delivery_failed')
        self.assertEqual(len(self._transfers(shipment, 'cod_return')), 1)
        self.assertAlmostEqual(
            self.env['logistics.account.transfer'].get_seller_cod_pending_balance(self.seller),
            0.0, places=2)
        self.assertAlmostEqual(self.wallet.balance, 500.0, places=2)
        shipment._kx_reverse_customer_cancel()
        self.assertEqual(len(self._transfers(shipment, 'cod_return')), 1)
        self.assertAlmostEqual(self.wallet.balance, 500.0, places=2)

        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'out_for_delivery',
        })
        shipment.action_mark_delivered()
        self.assertEqual(shipment.state, 'delivered')
        self.assertFalse(self._transfers(shipment, 'cod_return'))
        self.assertAlmostEqual(
            self.env['logistics.account.transfer'].get_seller_cod_pending_balance(self.seller),
            320.0, places=2)
        self.assertAlmostEqual(self.wallet.balance, 452.0, places=2)

    def test_hub_receive_from_the_seller_credits_cod_once(self):
        shipment = self._shipment(cod_amount=199.0, total_order_value=199.0)
        self._charge_wallet(shipment, 50.0)
        hub = shipment.source_hub_id or shipment.get_preferred_portal_drop_hub()
        self.assertTrue(hub)
        shipment.action_hub_receive(hub=hub)
        self.assertEqual(shipment.custodian_type, 'hub')
        self.assertTrue(shipment.picked_on)
        self.assertEqual(len(self._transfers(shipment, 'cod_pickup')), 1)
        self.assertAlmostEqual(
            self.env['logistics.account.transfer'].get_seller_cod_pending_balance(self.seller),
            199.0, places=2)
        self.assertAlmostEqual(self.wallet.balance, 450.0, places=2)
        self.assertFalse(self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', 'KX-DLV-REFUND:%s' % shipment.id),
        ]))
        shipment.action_hub_receive(hub=hub)
        self.assertEqual(len(self._transfers(shipment, 'cod_pickup')), 1)

    def test_destination_hub_can_receive_before_the_source_hub(self):
        mal = self.env.ref('keralariders_logistics.hub_malappuram')
        shipment = self._shipment(
            cod_amount=999.0,
            total_order_value=999.0,
            shipping_to_zip=self._pin('malappuram'),
        )
        shipment.write({'shipping_from_zip': self._pin('ernakulam')})
        shipment.action_mark_picked()
        self.assertEqual(shipment.destination_hub_id, mal)
        self.assertNotEqual(shipment.get_preferred_portal_drop_hub(), mal)
        shipment.action_hub_receive(hub=mal)
        self.assertEqual(shipment.current_hub_id, mal)
        self.assertEqual(shipment.state, 'at_destination_hub')
        self.assertEqual(len(self._transfers(shipment, 'cod_pickup')), 1)
        transfer = shipment.estimated_route_ids.filtered(
            lambda route: route.operation_type == 'hub_transfer'
        )
        self.assertTrue(transfer)
        self.assertEqual(set(transfer.mapped('state')), {'done'})
        self.assertEqual(shipment.active_leg_id.operation_type, 'delivery')
        rider = self.env['logistics.delivery.executive'].create({
            'name': 'Destination Delivery Rider',
            'mobile': '9000004444',
            'is_pickup': True,
            'is_delivery': True,
            'is_driver': False,
        })
        self.assertTrue(shipment.can_de_self_assign(rider))
        shipment.action_assign_delivery_executive(rider)

    def test_wrong_hub_scan_of_a_kozhikode_parcel_writes_no_wallet_line(self):
        koz = self.env.ref('keralariders_logistics.hub_kozhikode')
        mal = self.env.ref('keralariders_logistics.hub_malappuram')
        shipment = self._shipment(
            cod_amount=210.0,
            total_order_value=210.0,
            shipping_to_zip=self._pin('kozhikode'),
        )
        shipment.write({'shipping_from_zip': self._pin('malappuram')})
        self.assertEqual(shipment.destination_hub_id, koz)
        self.assertEqual(shipment.source_hub_id, mal)
        self._charge_wallet(shipment, 48.0)
        shipment.action_mark_picked()
        shipment.action_hub_receive(hub=shipment.get_preferred_portal_drop_hub())
        self.assertEqual(shipment.get_preferred_portal_drop_hub(), koz)
        Wallet = self.env['logistics.wallet.transaction']
        Transfer = self.env['logistics.account.transfer']
        wallet_count = Wallet.search_count([('shipment_id', '=', shipment.id)])
        transfer_count = Transfer.search_count([('shipment_id', '=', shipment.id)])
        shipment.action_hub_receive(hub=mal)
        self.assertEqual(Wallet.search_count([('shipment_id', '=', shipment.id)]), wallet_count)
        self.assertEqual(Transfer.search_count([('shipment_id', '=', shipment.id)]), transfer_count)
        self.assertEqual(shipment.current_hub_id, mal)
        off_route = self.env.ref('keralariders_logistics.hub_ernakulam')
        self.assertNotIn(off_route, shipment.get_portal_drop_hub_ids())
        with self.assertRaises(UserError):
            shipment.action_hub_receive(hub=off_route)
        self.assertEqual(shipment.current_hub_id, mal)

    def test_india_post_can_still_be_received_off_the_next_hub(self):
        koz = self.env.ref('keralariders_logistics.hub_kozhikode')
        mal = self.env.ref('keralariders_logistics.hub_malappuram')
        ip_seller = self.env['logistics.seller'].create({
            'name': 'Network Ops India Post',
            'email': 'network.ops.ip@example.com',
            'zip': '682001',
        })
        shipment = self.env['logistics.shipment'].create({
            'seller_id': ip_seller.id,
            'shipping_to_name': 'IP Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': self._pin('kozhikode'),
            'shipping_to_mobile': '9876543210',
            'item_description': 'IP article',
            'total_weight': 0.4,
            'order_payment_type': 'prepaid',
            'total_order_value': 80.0,
        })
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'pickup_requested',
            'shipping_from_zip': self._pin('malappuram'),
        })
        self.assertEqual(shipment.fulfilment_method, 'indiapost')
        self.assertEqual(shipment.destination_hub_id, koz)
        self.assertEqual(shipment.source_hub_id, mal)
        shipment.action_mark_picked()
        self.assertNotEqual(shipment.get_preferred_portal_drop_hub(), koz)
        wallet_count = self.env['logistics.wallet.transaction'].search_count([
            ('shipment_id', '=', shipment.id),
        ])
        shipment.action_hub_receive(hub=koz)
        self.assertEqual(shipment.current_hub_id, koz)
        self.assertEqual(
            self.env['logistics.wallet.transaction'].search_count([
                ('shipment_id', '=', shipment.id),
            ]),
            wallet_count,
        )
        self.assertFalse(self._transfers(shipment, 'cod_pickup'))

    def test_return_requested_is_on_the_pickup_queue(self):
        Shipment = self.env['logistics.shipment']
        shipment = self._shipment(shipping_to_zip=self._pin('kozhikode'))
        hub = shipment.source_hub_id
        self.assertTrue(hub)
        self.assertIn(shipment, Shipment.search(hub._kx_awaiting_pickup_domain()))
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'return_requested',
            'is_return_journey': True,
        })
        self.assertIn(shipment, Shipment.search(hub._kx_awaiting_pickup_domain()))
        customer_hub = shipment.destination_hub_id
        self.assertTrue(customer_hub)
        self.assertIn(shipment, Shipment.search(customer_hub._kx_awaiting_pickup_domain()))
        self.env.cr.execute(
            "UPDATE logistics_shipment SET fulfilment_method = 'indiapost' WHERE id = %s",
            [shipment.id],
        )
        shipment.invalidate_recordset(['fulfilment_method'])
        self.assertIn(shipment, Shipment.search(hub._kx_awaiting_pickup_domain()))
        outbound = self._shipment(shipping_to_zip=self._pin('kozhikode'))
        self.env.cr.execute(
            "UPDATE logistics_shipment SET fulfilment_method = 'indiapost' WHERE id = %s",
            [outbound.id],
        )
        outbound.invalidate_recordset(['fulfilment_method'])
        self.assertNotIn(
            outbound, Shipment.search(outbound.source_hub_id._kx_awaiting_pickup_domain()))

    def test_rider_rows_list_pending_work_and_undeposited_cod(self):
        koz_pin = self._pin('kozhikode')
        pin = self.env['logistics.pincode'].search([('name', '=', koz_pin)], limit=1)
        self.executive.assigned_pickup_pincodes = [(4, pin.id)]
        waiting = self._shipment(shipping_to_zip=koz_pin)
        self.assertEqual(waiting.pickup_executive_id, self.executive)
        self.assertNotEqual(waiting.custodian_type, 'de')
        self.env['logistics.account.transfer'].create({
            'transfer_type': 'cod_payment',
            'from_account_id': self.company_account.id,
            'to_account_id': self.executive.default_cash_account_id.id,
            'amount': 40.0,
            'reference': 'Undeposited COD',
            'shipment_id': waiting.id,
            'related_seller_id': self.seller.id,
        })
        koz = self.env.ref('keralariders_logistics.hub_kozhikode')
        rows = [
            row for row in koz._kx_network_rider_rows()
            if row['executive'] == self.executive
        ]
        self.assertEqual(len(rows), 1)
        self.assertNotIn(waiting, rows[0]['shipments'])
        self.assertIn(waiting, rows[0]['pending'])
        self.assertAlmostEqual(rows[0]['undeposited_total'], 40.0, places=2)
        self.assertEqual(len(rows[0]['cash_lines']), 1)
        other = self.env['logistics.delivery.executive'].create({
            'name': 'Other Network Rider',
            'mobile': '9000002222',
            'is_delivery': True,
        })
        held = self._shipment(shipping_to_zip=koz_pin)
        self.env.cr.execute(
            "UPDATE logistics_shipment SET state = 'out_for_delivery', "
            "delivery_executive_id = %s, custodian_type = 'de', "
            "custodian_de_id = %s WHERE id = %s",
            [self.executive.id, other.id, held.id],
        )
        held.invalidate_recordset([
            'state', 'delivery_executive_id', 'custodian_type', 'custodian_de_id',
        ])
        again = [
            row for row in koz._kx_network_rider_rows()
            if row['executive'] == self.executive
        ]
        self.assertNotIn(held, again[0]['pending'])
        self.assertNotIn(held, again[0]['shipments'])
        holder = [
            row for row in koz._kx_network_rider_rows()
            if row['executive'] == other
        ]
        self.assertIn(held, holder[0]['shipments'])
