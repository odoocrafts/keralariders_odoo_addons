"""Extra pickup addresses are optional. A booking without one keeps the seller address."""

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestSalePoint(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Sale Point Seller',
            'zip': '682001',
            'street': 'Main Road',
            'phone': '9800000001',
        })
        cls.seller.write({'fulfilment_method': 'own_network'})
        cls.other = cls.env['logistics.seller'].create({
            'name': 'Other Sale Point Seller',
            'zip': '682001',
        })
        cls.other.write({'fulfilment_method': 'own_network'})
        cls.point = cls.env['logistics.seller.location'].create({
            'seller_id': cls.seller.id,
            'name': 'Shop Two',
            'street': 'Second Street',
            'zip': '695001',
            'phone': '9800000002',
        })

    def _shipment(self, **extra):
        vals = {
            'seller_id': self.seller.id,
            'shipping_to_name': 'Sale Point Customer',
            'shipping_to_address': '12 Test Road',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Parcel',
            'total_weight': 1.0,
        }
        vals.update(extra)
        return self.env['logistics.shipment'].create(vals)

    def test_no_sale_point_copies_the_seller_pincode(self):
        shipment = self._shipment()
        self.assertFalse(shipment.sale_point_id)
        self.assertEqual(shipment.shipping_from_zip, '682001')
        self.assertIn('Main Road', shipment.shipping_from_address or '')
        self.assertFalse(shipment.shipping_from_phone)

    def test_named_sale_point_copies_its_pincode_and_phone(self):
        shipment = self._shipment(sale_point_id=self.point.id)
        self.assertEqual(shipment.sale_point_id, self.point)
        self.assertEqual(shipment.shipping_from_zip, '695001')
        self.assertIn('Second Street', shipment.shipping_from_address or '')
        self.assertEqual(shipment.shipping_from_phone, '9800000002')
        self.assertEqual(shipment.shipping_from_name, 'Sale Point Seller')
        self.point.write({'zip': '682001', 'phone': '9800000003'})
        shipment.invalidate_recordset()
        self.assertEqual(shipment.shipping_from_zip, '695001')
        self.assertEqual(shipment.shipping_from_phone, '9800000002')

    def test_another_sellers_sale_point_is_rejected(self):
        foreign = self.env['logistics.seller.location'].create({
            'seller_id': self.other.id,
            'name': 'Foreign',
            'zip': '695001',
        })
        with self.assertRaises(UserError):
            self._shipment(sale_point_id=foreign.id)
