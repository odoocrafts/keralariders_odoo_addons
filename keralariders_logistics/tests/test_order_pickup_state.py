"""Parent order moves to Picked Up once a shipment is past pickup.

India Post often jumps pickup_requested → out_for_delivery without writing
``picked``. Own-network pickup still writes ``picked``. Both paths share
``logistics.order._compute_state``.
"""

from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


@tagged('post_install', '-at_install')
class TestOrderPickupState(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Order Pickup State Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})

    def _create_order_with_shipment(self, state='pickup_requested', **overrides):
        order = self.env['logistics.order'].create({
            'seller_id': self.seller.id,
        })
        vals = {
            'order_id': order.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'OFD Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 0.4,
            'length_cm': 20,
            'breadth_cm': 15,
            'height_cm': 10,
        }
        vals.update(overrides)
        shipment = self.env['logistics.shipment'].create(vals)
        if state and state != shipment.state:
            shipment.sudo().with_context(allow_shipment_state_write=True).write({
                'state': state,
            })
        return order, shipment

    def _add_shipment(self, order, state='pickup_requested', **overrides):
        vals = {
            'order_id': order.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'Second Customer',
            'shipping_to_address': '34 Test Road, Test Nagar',
            'shipping_to_zip': '682001',
            'shipping_to_mobile': '9876543211',
            'item_description': 'Second article',
            'total_weight': 0.4,
            'length_cm': 20,
            'breadth_cm': 15,
            'height_cm': 10,
        }
        vals.update(overrides)
        shipment = self.env['logistics.shipment'].create(vals)
        if state and state != shipment.state:
            shipment.sudo().with_context(allow_shipment_state_write=True).write({
                'state': state,
            })
        return shipment

    def _set_state(self, shipment, state):
        shipment.sudo()._write_with_state({'state': state})

    def test_order_stays_pickup_requested_until_shipment_ofd(self):
        order, shipment = self._create_order_with_shipment()
        self.assertEqual(order.state, 'pickup_requested')
        self.assertEqual(shipment.state, 'pickup_requested')

        self._set_state(shipment, 'out_for_delivery')
        self.assertEqual(shipment.state, 'out_for_delivery')
        self.assertEqual(order.state, 'picked')
        self.assertNotEqual(order.state, 'delivered')

    def test_in_transit_and_destination_hub_also_mark_picked(self):
        order, shipment = self._create_order_with_shipment()
        self._set_state(shipment, 'in_transit')
        self.assertEqual(order.state, 'picked')

        order2, shipment2 = self._create_order_with_shipment()
        self._set_state(shipment2, 'at_destination_hub')
        self.assertEqual(order2.state, 'picked')

    def test_own_network_picked_still_marks_order_picked(self):
        order, shipment = self._create_order_with_shipment()
        self._set_state(shipment, 'picked')
        self.assertEqual(order.state, 'picked')

    def test_one_ofd_does_not_fully_deliver_the_order(self):
        order, first = self._create_order_with_shipment()
        self._add_shipment(order)
        self._set_state(first, 'out_for_delivery')
        self.assertEqual(order.state, 'picked')
        self.assertNotEqual(order.state, 'delivered')

    def test_all_delivered_is_fully_delivered(self):
        order, shipment = self._create_order_with_shipment()
        self._set_state(shipment, 'out_for_delivery')
        self.assertEqual(order.state, 'picked')
        self._set_state(shipment, 'delivered')
        self.assertEqual(order.state, 'delivered')

    def test_partial_when_only_some_shipments_delivered(self):
        order, first = self._create_order_with_shipment()
        second = self._add_shipment(order)
        self._set_state(second, 'out_for_delivery')
        self._set_state(first, 'delivered')
        self.assertEqual(order.state, 'partial')

    def test_rewriting_ofd_is_idempotent(self):
        order, shipment = self._create_order_with_shipment()
        self._set_state(shipment, 'out_for_delivery')
        self.assertEqual(order.state, 'picked')
        self._set_state(shipment, 'out_for_delivery')
        self._set_state(shipment, 'out_for_delivery')
        self.assertEqual(shipment.state, 'out_for_delivery')
        self.assertEqual(order.state, 'picked')

    def test_draft_and_cancel_are_unchanged(self):
        order, shipment = self._create_order_with_shipment(state=None)
        self.assertEqual(shipment.state, 'order_added')
        self.assertEqual(order.state, 'draft')

        booked, booked_shipment = self._create_order_with_shipment()
        self._set_state(booked_shipment, 'cancelled')
        self.assertEqual(booked.state, 'cancelled')

        still_draft, _ = self._create_order_with_shipment(state=None)
        self.env['logistics.order']._advance_stuck_pickup_requested_orders()
        self.assertEqual(still_draft.state, 'draft')
        self.assertEqual(order.state, 'draft')

    def test_tracking_ofd_scan_advances_parent_order(self):
        order, shipment = self._create_order_with_shipment()
        shipment.sudo().write({'indiapost_article_number': 'EY547879055IN'})
        self.assertEqual(order.state, 'pickup_requested')

        self.env['logistics.indiapost.tracking']._ip_apply_tracking(shipment, {
            'tracking_details': [{
                'event': 'Out for delivery',
                'date': '20-09-2026',
                'time': '10:06',
                'office': 'Ernakulam HO',
                'officeid': '1',
            }],
        })
        self.assertEqual(shipment.state, 'out_for_delivery')
        self.assertEqual(order.state, 'picked')

        # Re-applying the same OFD scan must not error or regress.
        self.env['logistics.indiapost.tracking']._ip_apply_tracking(shipment, {
            'tracking_details': [{
                'event': 'Out for delivery',
                'date': '20-09-2026',
                'time': '10:06',
                'office': 'Ernakulam HO',
                'officeid': '1',
            }],
        })
        self.assertEqual(shipment.state, 'out_for_delivery')
        self.assertEqual(order.state, 'picked')

    def test_tracking_delivered_scan_fully_delivers_single_shipment_order(self):
        order, shipment = self._create_order_with_shipment(state='out_for_delivery')
        shipment.sudo().write({'indiapost_article_number': 'EY547879063IN'})
        self.env['logistics.indiapost.tracking']._ip_apply_tracking(shipment, {
            'del_status': 'Delivered',
            'tracking_details': [{
                'event': 'Item Delivered',
                'date': '21-09-2026',
                'time': '11:00',
                'office': 'Ernakulam HO',
                'officeid': '1',
            }],
        })
        self.assertEqual(shipment.state, 'delivered')
        self.assertEqual(order.state, 'delivered')

    def test_cron_catchup_advances_already_ofd_stuck_order(self):
        order, shipment = self._create_order_with_shipment()
        self._set_state(shipment, 'out_for_delivery')
        self.assertEqual(order.state, 'picked')

        self.env.cr.execute(
            'UPDATE logistics_order SET state = %s WHERE id = %s',
            ('pickup_requested', order.id),
        )
        order.invalidate_recordset(['state'])
        self.assertEqual(order.state, 'pickup_requested')

        flipped = self.env['logistics.order']._advance_stuck_pickup_requested_orders()
        self.assertIn(order, flipped)
        self.assertEqual(order.state, 'picked')

        # Second pass is a no-op.
        again = self.env['logistics.order']._advance_stuck_pickup_requested_orders()
        self.assertNotIn(order, again)
        self.assertEqual(order.state, 'picked')
