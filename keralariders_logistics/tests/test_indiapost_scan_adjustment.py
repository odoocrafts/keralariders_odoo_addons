"""India Post scan reweigh: wallet credit/debit, one post, portal, mail.

Hermetic: no India Post HTTP. Quotes used for a debit/credit are the
tracking ``tariff`` figure (trusted poll) or a patched tariff re-quote.
"""

from unittest.mock import patch

from odoo import fields as odoo_fields
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

ARTICLE = 'EY547878801IN'


class ScanAdjustmentMixin(IndiapostHermeticMixin):

    def _store_quote(self, shipment, total=118.0, base=100.0, vas=0.0, tax=18.0):
        shipment.write({
            'indiapost_base_tariff': base,
            'indiapost_vas_charges': vas,
            'indiapost_tax_amount': tax,
            'indiapost_total_tariff': total,
            'indiapost_quoted_weight_g': ipc.band_weight(
                ipc.kg_to_grams(shipment.total_weight)),
            'indiapost_tariff_quoted_on': odoo_fields.Datetime.now(),
            'indiapost_quote_signature': shipment._ip_quote_signature(),
        })

    def _debit_pickup(self, shipment, total=118.0):
        self._store_quote(shipment, total=total)
        shipment.action_add_wallet_transaction()
        self.assertTrue(shipment.wallet_transaction_id)
        return shipment.wallet_transaction_id

    def _adj_lines(self, shipment):
        return self.env['logistics.wallet.transaction'].search([
            ('shipment_id', '=', shipment.id),
            ('reference', '=', shipment._ip_scan_adj_reference()),
        ])

    def _scan_payload(self, article, **booking):
        details = {
            'article_number': article,
            'tariff': 0,
        }
        details.update(booking)
        return {
            'booking_details': details,
            'del_status': 'not delivered',
            'tracking_details': [{
                'event': 'Item Bagged',
                'date': '2026-09-21T00:00:00Z',
                'time': '10:06:00',
                'office': 'Kochi HO',
                'officeid': '22360020',
                'eventId': 'scan-adj-%s' % article,
            }],
        }

    def _apply_poll(self, shipment, **booking):
        Tracking = self.env['logistics.indiapost.tracking'].with_context(
            ip_allow_tariff_http=True, ip_trust_booking_tariff=True)
        Tracking._ip_apply_tracking(
            shipment, self._scan_payload(shipment.indiapost_article_number,
                                         **booking))


@tagged('post_install', '-at_install')
class TestIndiapostScanAdjustment(ScanAdjustmentMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Scan Adj Seller',
            'email': 'scan.adj.seller@example.com',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})
        cls.wallet = cls.seller.wallet_ids[0]

    def setUp(self):
        super().setUp()
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        self.wallet.invalidate_recordset(['balance'])
        self.order = self.env['logistics.order'].create({
            'seller_id': self.seller.id,
        })
        self.shipment = self._new_shipment()

    def _new_shipment(self, article=ARTICLE, **overrides):
        vals = {
            'order_id': self.order.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'Scan Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Scan article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        }
        vals.update(overrides)
        shipment = self.env['logistics.shipment'].create(vals)
        shipment.sudo().with_context(allow_shipment_state_write=True).write({
            'indiapost_article_number': article,
            'state': 'in_transit',
        })
        return shipment

    def test_extract_ignores_zero_tariff_and_missing_dims(self):
        extracted = self.env['logistics.indiapost.tracking']._ip_extract_scan_actuals({
            'booking_details': {
                'article_number': ARTICLE,
                'tariff': 0,
                'origin_pincode': '',
            },
            'tracking_details': [{'event': 'Item Bagged'}],
        })
        self.assertFalse(extracted.get('tariff'))
        self.assertFalse(extracted.get('weight_g'))
        self.assertFalse(extracted.get('length_cm'))

    def test_extract_reads_future_weight_and_tariff_keys(self):
        extracted = self.env['logistics.indiapost.tracking']._ip_extract_scan_actuals({
            'booking_details': {
                'article_number': ARTICLE,
                'physical_weight': 1800,
                'article_length': '32',
                'article_breadth': '20',
                'article_height': '16',
                'volumetric_weight': 2048,
                'tariff': 150.5,
            },
        })
        self.assertEqual(extracted['weight_g'], 1800)
        self.assertEqual(extracted['length_cm'], 32)
        self.assertEqual(extracted['breadth_cm'], 20)
        self.assertEqual(extracted['height_cm'], 16)
        self.assertEqual(extracted['volumetric_g'], 2048)
        self.assertAlmostEqual(extracted['tariff'], 150.5)

    def test_mismatch_debits_the_difference(self):
        self._debit_pickup(self.shipment, total=118.0)
        opening = self.wallet.balance
        self._apply_poll(self.shipment, tariff=150.0)

        lines = self._adj_lines(self.shipment)
        self.assertEqual(len(lines), 1)
        self.assertAlmostEqual(lines.amount, -32.0, places=2)
        self.assertEqual(lines.transaction_type, 'debit')
        self.assertIn('India Post rate adjustment', lines.description)
        self.assertIn(self.shipment.name, lines.description)
        self.assertTrue(self.shipment.indiapost_scan_adjusted)
        self.assertAlmostEqual(self.shipment.indiapost_scan_difference, 32.0,
                               places=2)
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, opening - 32.0, places=2)

    def test_mismatch_credits_the_difference(self):
        self._debit_pickup(self.shipment, total=118.0)
        self._apply_poll(self.shipment, tariff=100.0)

        lines = self._adj_lines(self.shipment)
        self.assertEqual(len(lines), 1)
        self.assertAlmostEqual(lines.amount, 18.0, places=2)
        self.assertEqual(lines.transaction_type, 'credit')
        self.assertAlmostEqual(self.shipment.indiapost_scan_difference, -18.0,
                               places=2)

    def test_no_double_post_on_repeat_poll(self):
        self._debit_pickup(self.shipment, total=118.0)
        self._apply_poll(self.shipment, tariff=150.0)
        self._apply_poll(self.shipment, tariff=150.0)
        self._apply_poll(self.shipment, tariff=999.0)
        self.assertEqual(len(self._adj_lines(self.shipment)), 1)
        self.assertAlmostEqual(self._adj_lines(self.shipment).amount, -32.0,
                               places=2)

    def test_no_wallet_line_when_amounts_match(self):
        self._debit_pickup(self.shipment, total=118.0)
        self._apply_poll(self.shipment, tariff=118.0)
        self.assertFalse(self._adj_lines(self.shipment))
        self.assertTrue(self.shipment.indiapost_scan_adjusted)
        self.assertAlmostEqual(self.shipment.indiapost_scan_difference, 0.0,
                               places=2)
        self.assertFalse(self.env['mail.mail'].search([
            ('res_id', '=', self.shipment.id),
            ('model', '=', 'logistics.shipment'),
            ('subject', 'ilike', 'India Post updated'),
        ]))

    def test_dims_change_same_quote_emails_without_wallet(self):
        self._debit_pickup(self.shipment, total=118.0)

        def fake_quote(*args, **kwargs):
            return {
                'ok': True,
                'base_tariff': 100.0,
                'vas_charges': 0.0,
                'total_tax': 18.0,
                'final_amount': 118.0,
                'total_payable': 118.0,
                'billed_weight_g': 1600,
                'chargeable_weight_g': 2048,
                'volumetric_weight_g': 2048,
                'actual_weight_g': 1600,
            }

        with patch.object(
                self.registry['logistics.indiapost.tariff'],
                'quote', autospec=True, side_effect=fake_quote):
            self._apply_poll(
                self.shipment,
                physical_weight=1600,
                article_length=32,
                article_breadth=20,
                article_height=16,
            )

        self.assertFalse(self._adj_lines(self.shipment))
        self.assertTrue(self.shipment.indiapost_scan_seen)
        self.assertEqual(self.shipment.indiapost_scan_weight_g, 1600)
        self.assertEqual(self.shipment.indiapost_scan_length_cm, 32)
        mail = self.env['mail.mail'].search([
            ('res_id', '=', self.shipment.id),
            ('model', '=', 'logistics.shipment'),
            ('state', '=', 'outgoing'),
        ])
        self.assertEqual(len(mail), 1)
        self.assertIn('scan.adj.seller@example.com', mail.email_to)
        self.assertIn('No wallet movement', mail.body_html)

    def test_mail_queued_on_wallet_move(self):
        self._debit_pickup(self.shipment, total=118.0)
        self._apply_poll(self.shipment, tariff=150.0)
        mail = self.env['mail.mail'].search([
            ('res_id', '=', self.shipment.id),
            ('model', '=', 'logistics.shipment'),
            ('state', '=', 'outgoing'),
        ])
        self.assertEqual(len(mail), 1)
        self.assertIn('India Post updated', mail.subject)
        self.assertIn(self.shipment.name, mail.subject)
        self.assertIn('ARN', mail.body_html)
        self.assertIn('Debit', mail.body_html)
        self.assertIn('notifications@keralaxpress.com', (mail.email_from or '').lower())
        self.assertEqual(mail.state, 'outgoing')

    def test_cod_shipping_charge_still_adjusts(self):
        shipment = self._new_shipment(
            article='EY547878819IN',
            order_payment_type='cod',
            total_order_value=299.0,
            cod_amount=299.0,
        )
        self._debit_pickup(shipment, total=118.0)
        self._apply_poll(shipment, tariff=150.0)
        lines = self._adj_lines(shipment)
        self.assertEqual(len(lines), 1)
        self.assertAlmostEqual(lines.amount, -32.0, places=2)
        self.assertFalse(self.env['logistics.account.transfer'].search([
            ('shipment_id', '=', shipment.id),
            ('transfer_type', '=', 'cod_payment'),
        ]))

    def test_skip_return_journey_and_hub_network(self):
        self._debit_pickup(self.shipment, total=118.0)
        self.shipment.sudo().write({'is_return_journey': True})
        self._apply_poll(self.shipment, tariff=150.0)
        self.assertFalse(self._adj_lines(self.shipment))

        own = self._new_shipment(article='EY547878827IN')
        own.sudo().with_context(allow_fulfilment_method_write=True).write({
            'fulfilment_method': 'own_network',
        })
        # Hub network is billed from the slab; still give it a wallet line so
        # a mistaken apply would have something to credit against.
        own.action_add_wallet_transaction()
        self._apply_poll(own, tariff=150.0)
        self.assertFalse(self._adj_lines(own))

    def test_skip_without_original_pickup_debit(self):
        self._store_quote(self.shipment, total=118.0)
        self._apply_poll(self.shipment, tariff=150.0)
        self.assertFalse(self._adj_lines(self.shipment))
        self.assertFalse(self.shipment.indiapost_scan_seen)

    def test_webhook_tariff_is_not_posted_to_the_wallet(self):
        self._debit_pickup(self.shipment, total=118.0)
        charges_before = self.shipment.delivery_charges_total
        self.env['logistics.indiapost.tracking'].ingest_webhook('other', {
            'articleNumber': self.shipment.indiapost_article_number,
            'event': 'Item Bagged',
            'eventId': 'webhook-tariff-1',
            'tariff': 9999,
            'calculated_tariff': 9999,
            'delivery_charges_total': 1,
        })
        self.assertFalse(self._adj_lines(self.shipment))
        self.assertEqual(self.shipment.delivery_charges_total, charges_before)
        self.assertFalse(self.shipment.indiapost_scan_adjusted)

    def test_webhook_dims_stay_pending_until_cron(self):
        self._debit_pickup(self.shipment, total=118.0)

        def fake_quote(*args, **kwargs):
            return {
                'ok': True,
                'base_tariff': 130.0,
                'vas_charges': 0.0,
                'total_tax': 23.4,
                'final_amount': 153.4,
                'total_payable': 153.4,
                'billed_weight_g': 1800,
                'chargeable_weight_g': 2048,
                'volumetric_weight_g': 2048,
                'actual_weight_g': 1800,
            }

        self.env['logistics.indiapost.tracking'].ingest_webhook('other', {
            'articleNumber': self.shipment.indiapost_article_number,
            'event': 'Item Received',
            'eventId': 'webhook-dims-1',
            'physical_weight': 1800,
            'article_length': 32,
            'article_breadth': 20,
            'article_height': 16,
        })
        self.assertTrue(self.shipment.indiapost_scan_seen)
        self.assertEqual(self.shipment.indiapost_scan_weight_g, 1800)
        self.assertTrue(self.shipment.indiapost_scan_quote_pending)
        self.assertFalse(self._adj_lines(self.shipment))

        with patch.object(
                self.registry['logistics.indiapost.tariff'],
                'quote', autospec=True, side_effect=fake_quote):
            self.env['logistics.indiapost.tracking']._ip_apply_pending_scan_adjustments()

        lines = self._adj_lines(self.shipment)
        self.assertEqual(len(lines), 1)
        self.assertAlmostEqual(lines.amount, -(153.4 - 118.0), places=2)
        self.assertFalse(self.shipment.indiapost_scan_quote_pending)


@tagged('post_install', '-at_install')
class TestIndiapostScanAdjustmentPortal(ScanAdjustmentMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Scan Adj Portal Seller',
            'email': 'scan.adj.portal@example.com',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.env['logistics.wallet.transaction'].create({
            'wallet_id': cls.wallet.id,
            'amount': 5000.0,
            'reference': 'Test top-up',
        })
        cls.login = 'kx_scan_adj_portal'
        cls.env['res.users'].create({
            'name': 'Scan Adj Portal',
            'login': cls.login,
            'password': cls.login,
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        order = cls.env['logistics.order'].create({'seller_id': cls.seller.id})
        cls.shipment = cls.env['logistics.shipment'].create({
            'order_id': order.id,
            'seller_id': cls.seller.id,
            'shipping_to_name': 'Scan Portal Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Scan portal article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        })
        cls.shipment.sudo().with_context(
            allow_shipment_state_write=True,
            allow_delivery_charge_write=True,
        ).write({
            'indiapost_article_number': 'EY547878835IN',
            'state': 'in_transit',
            'indiapost_base_tariff': 100.0,
            'indiapost_vas_charges': 0.0,
            'indiapost_tax_amount': 18.0,
            'indiapost_total_tariff': 118.0,
            'indiapost_quoted_weight_g': 1500,
            'indiapost_tariff_quoted_on': odoo_fields.Datetime.now(),
            'indiapost_quote_signature': cls.shipment._ip_quote_signature(),
        })
        cls.shipment.action_add_wallet_transaction()
        cls.env['logistics.indiapost.tracking'].with_context(
            ip_allow_tariff_http=True, ip_trust_booking_tariff=True,
        )._ip_apply_tracking(cls.shipment, {
            'booking_details': {
                'article_number': cls.shipment.indiapost_article_number,
                'tariff': 150.0,
                'physical_weight': 1800,
                'article_length': 32,
                'article_breadth': 20,
                'article_height': 16,
            },
            'tracking_details': [{
                'event': 'Item Bagged',
                'date': '2026-09-21T00:00:00Z',
                'time': '11:00:00',
                'office': 'Kochi HO',
                'officeid': '1',
                'eventId': 'portal-scan-1',
            }],
        })

    def test_order_and_shipment_detail_show_scan_fields(self):
        self.assertTrue(self.shipment.portal_indiapost_scan_visible())
        self.authenticate(self.login, self.login)
        order_page = self.url_open('/my/orders/%s' % self.shipment.order_id.id)
        self.assertEqual(order_page.status_code, 200)
        self.assertIn('India Post scan update', order_page.text)
        self.assertIn('At pickup', order_page.text)
        self.assertIn('After scan', order_page.text)
        self.assertIn('Wallet debit', order_page.text)

        hidden = self.env['logistics.shipment'].create({
            'order_id': self.shipment.order_id.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'No Scan Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543211',
            'item_description': 'No scan yet',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        })
        self.assertFalse(hidden.portal_indiapost_scan_visible())

        detail = self.url_open('/my/shipments/%s' % self.shipment.id)
        self.assertEqual(detail.status_code, 200)
        self.assertIn('India Post scan update', detail.text)
        self.assertIn('Quoted charge', detail.text)
        listing = self.url_open('/my/shipments')
        self.assertEqual(listing.status_code, 200)
        self.assertIn('/my/shipments/%s' % self.shipment.id, listing.text)
