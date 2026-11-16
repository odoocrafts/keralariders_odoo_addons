"""KeralaXpress poster rate card. India Post does not use these slabs."""

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestKxPosterTariff(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Charges = cls.env['logistics.delivery.charges']
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Poster Tariff Seller',
            'zip': '682001',
        })
        if 'fulfilment_method' in cls.seller._fields:
            cls.seller.with_context(allow_fulfilment_method_write=True).write({
                'fulfilment_method': 'own_network',
            })

    def test_poster_slabs_ignore_the_district_border(self):
        expected = (
            (0.5, 50.0),
            (1.0, 50.0),
            (1.5, 70.0),
            (2.0, 100.0),
            (3.0, 125.0),
            (4.0, 150.0),
            (5.0, 170.0),
            (5.1, 190.0),
            (6.0, 190.0),
        )
        for weight, amount in expected:
            same = self.Charges.calculate_delivery_charge(weight, True)
            other = self.Charges.calculate_delivery_charge(weight, False)
            self.assertEqual(same, amount, 'weight %s' % weight)
            self.assertEqual(other, amount, 'weight %s across a district' % weight)

    def test_a_seller_package_is_not_the_poster(self):
        package = self.env['logistics.delivery.package'].create({
            'name': 'Seller Specific Card',
            'is_default': False,
        })
        self.Charges.create({
            'package_id': package.id,
            'minimum_weight': 0.0,
            'maximum_weight': 10.0,
            'same_district_amount': 9.0,
            'different_district_amount': 11.0,
        })
        self.assertEqual(
            self.Charges.calculate_delivery_charge(1.0, True, package_id=package.id),
            9.0,
        )
        self.assertEqual(
            self.Charges.calculate_delivery_charge(1.0, False, package_id=package.id),
            11.0,
        )
        self.assertEqual(self.Charges.calculate_delivery_charge(1.0, True), 50.0)

    def test_open_network_quotes_are_repriced_and_manual_prices_are_not(self):
        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'Poster Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Poster article',
            'total_weight': 1.0,
        })
        self.assertEqual(shipment.fulfilment_method, 'own_network')
        self.assertEqual(shipment.state, 'order_added')
        self.assertEqual(shipment.delivery_charges_subtotal, 50.0)

        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE logistics_shipment SET delivery_charges_subtotal = 1 "
            "WHERE id = %s",
            [shipment.id],
        )
        shipment.invalidate_recordset(['delivery_charges_subtotal'])
        self.env['logistics.shipment']._kx_recompute_open_network_charges()
        shipment.invalidate_recordset(['delivery_charges_subtotal'])
        self.assertEqual(shipment.delivery_charges_subtotal, 50.0)

        kept = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'Manual Price Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543211',
            'item_description': 'Manual article',
            'total_weight': 1.0,
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE logistics_shipment "
            "SET delivery_charges_subtotal = 1, "
            "    delivery_charge_override_signature = 'keep' "
            "WHERE id = %s",
            [kept.id],
        )
        kept.invalidate_recordset([
            'delivery_charges_subtotal', 'delivery_charge_override_signature',
        ])
        self.env['logistics.shipment']._kx_recompute_open_network_charges()
        kept.invalidate_recordset(['delivery_charges_subtotal'])
        self.assertEqual(kept.delivery_charges_subtotal, 1.0)

        ip = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'Left On The Postal Price',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543212',
            'item_description': 'India Post article',
            'total_weight': 1.0,
        })
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE logistics_shipment "
            "SET fulfilment_method = 'indiapost', delivery_charges_subtotal = 7 "
            "WHERE id = %s",
            [ip.id],
        )
        ip.invalidate_recordset(['fulfilment_method', 'delivery_charges_subtotal'])
        self.assertEqual(ip.fulfilment_method, 'indiapost')
        self.env['logistics.shipment']._kx_recompute_open_network_charges()
        ip.invalidate_recordset(['delivery_charges_subtotal'])
        self.assertEqual(ip.delivery_charges_subtotal, 7.0)
