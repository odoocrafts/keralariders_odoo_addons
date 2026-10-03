"""Paste a WhatsApp order message into the seller portal.

The copy button and the parser share ``WHATSAPP_ORDER_TEMPLATE``. Bulk create
reuses the draft shipment path: Order Added, no pickup, no wallet debit.
A block that fails to parse is reported and does not cancel the others.
"""

import html
import json
import re

from odoo import fields
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.models.whatsapp_order_paste import (
    WHATSAPP_ORDER_TEMPLATE,
    parse_whatsapp_orders,
)
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


def _ok_posts(text):
    return [block['post'] for block in parse_whatsapp_orders(text) if block['ok']]


@tagged('post_install', '-at_install')
class TestWhatsappOrderPaste(TransactionCase):

    def test_one_valid_message_extracts_the_form_fields(self):
        blocks = parse_whatsapp_orders(WHATSAPP_ORDER_TEMPLATE)
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0]['ok'], blocks[0]['reason'])
        post = blocks[0]['post']
        self.assertEqual(post['shipping_to_name'], 'Sainabi AC')
        self.assertEqual(post['shipping_to_mobile'], '9496427718')
        self.assertEqual(
            post['shipping_to_address'],
            'Awrechetta house, Near yousuf palli',
        )
        self.assertEqual(post['shipping_to_zip'], '682552')
        self.assertEqual(post['total_weight'], '0.5')
        self.assertEqual(post['length_cm'], '20')
        self.assertEqual(post['breadth_cm'], '15')
        self.assertEqual(post['height_cm'], '10')
        self.assertEqual(post['order_payment_type'], 'prepaid')
        self.assertEqual(post['total_order_value'], '0')
        self.assertEqual(post['item_description'], 'Clothes')
        self.assertNotIn('shipping_to_state_name', post)
        self.assertNotIn('indiapost_article_type', post)

    def test_label_case_and_separator_still_parse(self):
        text = (
            "name - Sainabi AC\n"
            "MOBILE: 9496427718\n"
            "Address: Awrechetta house\n"
            "Near yousuf palli\n"
            "Pin code : 682552\n"
            "weight g: 500\n"
            "Item - Clothes"
        )
        post = _ok_posts(text)[0]
        self.assertEqual(post['shipping_to_name'], 'Sainabi AC')
        self.assertEqual(post['shipping_to_mobile'], '9496427718')
        self.assertEqual(
            post['shipping_to_address'],
            'Awrechetta house Near yousuf palli',
        )
        self.assertEqual(post['shipping_to_zip'], '682552')
        self.assertEqual(post['total_weight'], '0.5')
        self.assertEqual(post['item_description'], 'Clothes')
        self.assertEqual(post['order_payment_type'], 'prepaid')

    def test_blank_line_splits_two_messages(self):
        text = (
            WHATSAPP_ORDER_TEMPLATE
            + "\n\n"
            + WHATSAPP_ORDER_TEMPLATE.replace('Sainabi AC', 'Second Customer')
        )
        blocks = parse_whatsapp_orders(text)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(all(block['ok'] for block in blocks))
        self.assertEqual(
            [block['post']['shipping_to_name'] for block in blocks],
            ['Sainabi AC', 'Second Customer'],
        )

    def test_rule_line_splits_two_messages(self):
        text = (
            WHATSAPP_ORDER_TEMPLATE
            + "\n---\n"
            + WHATSAPP_ORDER_TEMPLATE.replace('Sainabi AC', 'Second Customer')
        )
        blocks = parse_whatsapp_orders(text)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(all(block['ok'] for block in blocks))
        self.assertEqual(blocks[1]['post']['shipping_to_name'], 'Second Customer')

    def test_missing_mobile_is_reported_and_not_ok(self):
        text = WHATSAPP_ORDER_TEMPLATE.replace('Mobile: 9496427718\n', '')
        blocks = parse_whatsapp_orders(text)
        self.assertEqual(len(blocks), 1)
        self.assertFalse(blocks[0]['ok'])
        self.assertIn('Mobile', blocks[0]['reason'])
        self.assertNotIn('shipping_to_mobile', blocks[0]['post'])

    def test_missing_pincode_is_reported_and_not_ok(self):
        text = WHATSAPP_ORDER_TEMPLATE.replace('Pincode: 682552\n', '')
        blocks = parse_whatsapp_orders(text)
        self.assertEqual(len(blocks), 1)
        self.assertFalse(blocks[0]['ok'])
        self.assertIn('Pincode', blocks[0]['reason'])
        self.assertNotIn('shipping_to_zip', blocks[0]['post'])

    def test_a_bad_block_does_not_drop_a_good_one(self):
        text = (
            WHATSAPP_ORDER_TEMPLATE
            + "\n---\n"
            + "Name: No Phone\nAddress: 1 Road\nPincode: 695001\n"
            + "Weight g: 500\nItem: Hat\n"
        )
        blocks = parse_whatsapp_orders(text)
        self.assertEqual(len(blocks), 2)
        self.assertTrue(blocks[0]['ok'])
        self.assertFalse(blocks[1]['ok'])
        self.assertIn('Mobile', blocks[1]['reason'])

    def test_payment_cod_and_prepaid_are_recognized(self):
        prepaid = _ok_posts(
            WHATSAPP_ORDER_TEMPLATE.replace('Payment: Prepaid', 'PAYMENT: PREPAID')
        )[0]
        self.assertEqual(prepaid['order_payment_type'], 'prepaid')

        cod_text = WHATSAPP_ORDER_TEMPLATE.replace(
            'Payment: Prepaid', 'Payment: COD',
        ).replace('COD amount: 0', 'COD amount: 1500')
        cod = _ok_posts(cod_text)[0]
        self.assertEqual(cod['order_payment_type'], 'cod')
        self.assertEqual(cod['total_order_value'], '1500')

        words = _ok_posts(
            WHATSAPP_ORDER_TEMPLATE.replace(
                'Payment: Prepaid', 'Payment - cash on delivery',
            ).replace('COD amount: 0', 'COD amount: 250')
        )[0]
        self.assertEqual(words['order_payment_type'], 'cod')
        self.assertEqual(words['total_order_value'], '250')

        missing_amount = parse_whatsapp_orders(
            WHATSAPP_ORDER_TEMPLATE.replace('Payment: Prepaid', 'Payment: COD').replace(
                'COD amount: 0\n', '',
            )
        )
        self.assertFalse(missing_amount[0]['ok'])
        self.assertIn('COD amount', missing_amount[0]['reason'])


@tagged('post_install', '-at_install')
class TestPortalWhatsappOrders(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'WhatsApp Order Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'own_network'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.portal_login = 'kx_wa_order'
        cls.env['res.users'].create({
            'name': 'WhatsApp Order Portal',
            'login': cls.portal_login,
            'password': cls.portal_login,
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def setUp(self):
        super().setUp()
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        self.wallet.invalidate_recordset(['balance'])

    def _csrf(self, html_text):
        match = re.search(r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html_text)
        self.assertTrue(match, 'no csrf_token in the rendered page')
        return match.group(1)

    def _hidden_value(self, html_text, name):
        match = re.search(
            r'name="%s"[^>]*\bvalue="([^"]*)"' % re.escape(name), html_text)
        self.assertTrue(match, 'no %s in the rendered page' % name)
        return match.group(1)

    def _orders_of(self):
        return self.env['logistics.order'].search([
            ('seller_id', '=', self.seller.id),
        ])

    def _template_text(self, page_html):
        match = re.search(
            r'kx-wa-template-text[^>]*>(.*?)</textarea>',
            page_html,
            re.DOTALL,
        )
        self.assertTrue(match, 'copyable template textarea missing')
        return html.unescape(match.group(1))

    def test_add_order_and_bulk_pages_offer_the_same_template(self):
        self.authenticate(self.portal_login, self.portal_login)
        manual = self.url_open('/my/orders/manual')
        self.assertEqual(manual.status_code, 200)
        self.assertIn('Copy WhatsApp template', manual.text)
        self.assertIn('replace the example', manual.text.lower())
        self.assertEqual(self._template_text(manual.text), WHATSAPP_ORDER_TEMPLATE)
        self.assertIn('id="kx_wa_paste"', manual.text)
        self.assertIn('Create Order', manual.text)
        self.assertIn('name="shipping_to_name"', manual.text)

        bulk = self.url_open('/my/orders/new')
        self.assertEqual(bulk.status_code, 200)
        self.assertIn('Copy WhatsApp template', bulk.text)
        self.assertEqual(self._template_text(bulk.text), WHATSAPP_ORDER_TEMPLATE)
        self.assertIn('name="csv_file"', bulk.text)
        self.assertIn('Download CSV Template', bulk.text)
        self.assertIn('id="kx_wa_bulk_form"', bulk.text)
        self.assertIn('name="whatsapp_create_token"', bulk.text)
        self.assertIn('---', bulk.text)

    def test_two_messages_create_two_drafts_and_skip_the_bad_block(self):
        self.authenticate(self.portal_login, self.portal_login)
        opening = self.wallet.balance
        form = self.url_open('/my/orders/new')
        message = "\n".join([
            "Name: First Customer",
            "Mobile: 9876543210",
            "Address: 12 Test Road",
            "Pincode: 695001",
            "Weight g: 500",
            "Length cm: 20",
            "Breadth cm: 15",
            "Height cm: 10",
            "Payment: Prepaid",
            "COD amount: 0",
            "Item: Clothes",
            "",
            "Name: Second Customer",
            "Mobile: 9876543211",
            "Address: 13 Test Road",
            "Pincode: 695014",
            "Weight g: 1500",
            "Length cm: 20",
            "Breadth cm: 15",
            "Height cm: 10",
            "Payment: COD",
            "COD amount: 250",
            "Item: Books",
            "---",
            "Name: Broken Customer",
            "Address: 14 Test Road",
            "Pincode: 695001",
            "Weight g: 500",
            "Item: Hat",
        ])
        preview = self.url_open('/my/orders/whatsapp_preview', data={
            'csrf_token': self._csrf(form.text),
            'message': message,
        })
        self.assertEqual(preview.status_code, 200)
        payload = json.loads(preview.text)
        self.assertEqual(payload['ok_count'], 2)
        self.assertEqual(payload['fail_count'], 1)
        self.assertIn('Mobile', payload['blocks'][2]['reason'])
        self.env.invalidate_all()
        self.assertFalse(self._orders_of(), 'preview must not create orders')

        created = self.url_open('/my/orders/whatsapp_create', data={
            'csrf_token': self._csrf(form.text),
            'whatsapp_create_token': self._hidden_value(
                form.text, 'whatsapp_create_token'),
            'pickup_date': fields.Date.context_today(self.env.user).isoformat(),
            'message': message,
        })
        self.assertEqual(created.status_code, 200)
        self.assertIn('Created 2 draft orders', created.text)
        self.assertIn('Mobile', created.text)

        self.env.invalidate_all()
        orders = self._orders_of()
        self.assertEqual(len(orders), 2)
        self.assertEqual(set(orders.mapped('state')), {'draft'})
        shipments = orders.mapped('shipment_ids')
        self.assertEqual(len(shipments), 2)
        self.assertEqual(set(shipments.mapped('state')), {'order_added'})
        self.assertEqual(set(shipments.mapped('fulfilment_method')), {'own_network'})
        by_name = {shipment.shipping_to_name: shipment for shipment in shipments}
        self.assertEqual(set(by_name), {'First Customer', 'Second Customer'})
        self.assertNotIn('Broken Customer', by_name)
        self.assertEqual(by_name['First Customer'].total_weight, 0.5)
        self.assertEqual(by_name['First Customer'].order_payment_type, 'prepaid')
        self.assertEqual(by_name['First Customer'].shipping_to_zip, '695001')
        second = by_name['Second Customer']
        self.assertEqual(second.total_weight, 1.5)
        self.assertEqual(second.order_payment_type, 'cod')
        self.assertEqual(second.cod_amount, 250.0)
        self.assertEqual(second.total_order_value, 250.0)
        self.assertFalse(shipments.filtered('pickup_requested_on'))
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, opening, places=2)
        self.assertFalse(self.env['logistics.wallet.transaction'].search([
            ('wallet_id', '=', self.wallet.id),
            ('shipment_id', 'in', shipments.ids),
        ]))
