"""Seller badges and backend actions ignore unfinished draft / unbooked work."""

from lxml import etree

from odoo.tests import TransactionCase, tagged
from odoo.tools.safe_eval import safe_eval


@tagged('post_install', '-at_install')
class TestSellerDraftCounts(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Draft Count Seller',
            'zip': '682001',
        })

    def _create_order_with_shipment(self):
        order = self.env['logistics.order'].create({
            'seller_id': self.seller.id,
        })
        shipment = self.env['logistics.shipment'].create({
            'order_id': order.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'Draft Count Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.5,
        })
        return order, shipment

    def _book(self, shipment):
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'pickup_requested',
        })

    def _action_context(self, xmlid):
        action = self.env.ref(xmlid)
        ctx = action.context
        if not ctx:
            return {}
        if isinstance(ctx, dict):
            return ctx
        return safe_eval(ctx)

    def _search_filter_domain(self, xmlid, filter_name):
        arch = etree.fromstring(self.env.ref(xmlid).arch_db)
        for node in arch.xpath('//filter'):
            if node.get('name') == filter_name:
                return node.get('domain')
        return None

    def test_draft_order_and_unbooked_shipment_are_omitted_from_badges(self):
        order, shipment = self._create_order_with_shipment()
        self.assertEqual(order.state, 'draft')
        self.assertEqual(shipment.state, 'order_added')

        self.seller.invalidate_recordset(['orders_count', 'shipments_count'])
        self.assertEqual(self.seller.orders_count, 0)
        self.assertEqual(self.seller.shipments_count, 0)

        self.assertEqual(
            self.env['logistics.order'].search_count(
                [('seller_id', '=', self.seller.id)]
                + self.env['logistics.order']._domain_not_draft()
            ),
            0,
        )
        self.assertEqual(
            self.env['logistics.shipment'].search_count(
                [('seller_id', '=', self.seller.id)]
                + self.env['logistics.shipment']._domain_not_unbooked()
            ),
            0,
        )

    def test_booked_order_and_shipment_increment_badges(self):
        order, shipment = self._create_order_with_shipment()
        self._book(shipment)
        self.assertEqual(order.state, 'pickup_requested')
        self.assertEqual(shipment.state, 'pickup_requested')

        self.seller.invalidate_recordset(['orders_count', 'shipments_count'])
        self.assertEqual(self.seller.orders_count, 1)
        self.assertEqual(self.seller.shipments_count, 1)

        extra_order, extra_shipment = self._create_order_with_shipment()
        self.assertEqual(extra_order.state, 'draft')
        self.seller.invalidate_recordset(['orders_count', 'shipments_count'])
        self.assertEqual(self.seller.orders_count, 1)
        self.assertEqual(self.seller.shipments_count, 1)
        self.assertTrue(extra_shipment.exists())

    def test_cancelled_booked_orders_still_count(self):
        order, shipment = self._create_order_with_shipment()
        self._book(shipment)
        shipment.with_context(allow_shipment_state_write=True).write({
            'state': 'cancelled',
        })
        self.assertEqual(order.state, 'cancelled')
        self.seller.invalidate_recordset(['orders_count', 'shipments_count'])
        self.assertEqual(self.seller.orders_count, 1)
        self.assertEqual(self.seller.shipments_count, 1)

    def test_orders_action_hides_draft_by_default(self):
        ctx = self._action_context('keralariders_logistics.action_logistics_order')
        self.assertTrue(ctx.get('search_default_hide_draft'))
        domain = self._search_filter_domain(
            'keralariders_logistics.view_logistics_order_search',
            'hide_draft',
        )
        self.assertEqual(
            safe_eval(domain),
            self.env['logistics.order']._domain_not_draft(),
        )
        # Draft column / filter remains available when the default chip is cleared.
        self.assertTrue(self._search_filter_domain(
            'keralariders_logistics.view_logistics_order_search',
            'draft',
        ))

        order, _shipment = self._create_order_with_shipment()
        booked, booked_shipment = self._create_order_with_shipment()
        self._book(booked_shipment)

        hidden = self.env['logistics.order'].search(
            [('seller_id', '=', self.seller.id), ('state', '!=', 'draft')],
        )
        self.assertEqual(hidden, booked)
        self.assertIn(order, self.env['logistics.order'].search([
            ('seller_id', '=', self.seller.id),
        ]))

    def test_shipments_action_hides_unbooked_by_default(self):
        ctx = self._action_context('keralariders_logistics.action_shipment')
        self.assertTrue(ctx.get('search_default_hide_unbooked'))
        domain = self._search_filter_domain(
            'keralariders_logistics.view_logistics_shipment_search',
            'hide_unbooked',
        )
        self.assertEqual(
            safe_eval(domain),
            self.env['logistics.shipment']._domain_not_unbooked(),
        )
        self.assertTrue(self._search_filter_domain(
            'keralariders_logistics.view_logistics_shipment_search',
            'order_added',
        ))

        _order, unbooked = self._create_order_with_shipment()
        _booked_order, booked = self._create_order_with_shipment()
        self._book(booked)

        visible = self.env['logistics.shipment'].search(
            [('seller_id', '=', self.seller.id), ('state', '!=', 'order_added')],
        )
        self.assertEqual(visible, booked)
        self.assertIn(unbooked, self.env['logistics.shipment'].search([
            ('seller_id', '=', self.seller.id),
        ]))

    def test_seller_stat_buttons_use_the_same_default_filters(self):
        orders = self.seller.action_view_orders()
        self.assertTrue(orders['context'].get('search_default_hide_draft'))
        self.assertEqual(orders['domain'], [('seller_id', '=', self.seller.id)])

        shipments = self.seller.action_view_shipments()
        self.assertTrue(shipments['context'].get('search_default_hide_unbooked'))
        self.assertEqual(shipments['domain'], [('seller_id', '=', self.seller.id)])
