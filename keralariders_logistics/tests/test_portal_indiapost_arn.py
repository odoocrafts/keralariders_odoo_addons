"""Seller portal AWB / Ref shows the allocated India Post ARN only.

Hermetic: no India Post HTTP. The article number is written as booking would
store it; these tests never allocate a live barcode.
"""

import base64

from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin

ARTICLE = 'EA123456789IN'


class PortalIndiapostArnMixin(IndiapostHermeticMixin):

    @classmethod
    def _new_shipment(cls, seller, **overrides):
        order = cls.env['logistics.order'].create({'seller_id': seller.id})
        vals = {
            'order_id': order.id,
            'seller_id': seller.id,
            'shipping_to_name': 'ARN Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'ARN display article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        }
        vals.update(overrides)
        return cls.env['logistics.shipment'].create(vals)


@tagged('post_install', '-at_install')
class TestPortalIndiapostArn(PortalIndiapostArnMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'ARN IP Seller',
            'zip': '682001',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.hub_seller = cls.env['logistics.seller'].create({
            'name': 'ARN Hub Seller',
            'zip': '682001',
        })
        cls.hub_seller.write({'fulfilment_method': 'own_network'})

    def test_indiapost_with_allocated_arn(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
        })
        self.assertEqual(shipment.fulfilment_method, 'indiapost')
        self.assertEqual(shipment.portal_indiapost_arn(), ARTICLE)

    def test_indiapost_without_arn_is_hidden(self):
        shipment = self._new_shipment(self.ip_seller)
        self.assertEqual(shipment.fulfilment_method, 'indiapost')
        self.assertFalse(shipment.indiapost_article_number)
        self.assertFalse(shipment.portal_indiapost_arn())

    def test_hub_fulfilment_hides_arn(self):
        shipment = self._new_shipment(self.hub_seller)
        self.assertEqual(shipment.fulfilment_method, 'own_network')
        self.assertFalse(shipment.portal_indiapost_arn())

    def test_hub_leftover_article_number_is_hidden(self):
        shipment = self._new_shipment(self.hub_seller)
        shipment.sudo().write({'indiapost_article_number': ARTICLE})
        self.assertEqual(shipment.fulfilment_method, 'own_network')
        self.assertFalse(shipment.portal_indiapost_arn())

    def test_blank_article_number_is_hidden(self):
        shipment = self._new_shipment(self.ip_seller)
        shipment.sudo().write({'indiapost_article_number': '   '})
        self.assertFalse(shipment.portal_indiapost_arn())


@tagged('post_install', '-at_install')
class TestPortalIndiapostArnPages(PortalIndiapostArnMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)

        cls.ip_seller = cls.env['logistics.seller'].create({
            'name': 'ARN Portal IP Seller',
            'zip': '682001',
        })
        cls.ip_seller.write({'fulfilment_method': 'indiapost'})
        cls.ip_login = 'kx_arn_ip'
        cls.env['res.users'].create({
            'name': 'ARN Portal IP',
            'login': cls.ip_login,
            'password': cls.ip_login,
            'partner_id': cls.ip_seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        cls.ip_booked = cls._new_shipment(cls.ip_seller)
        cls.ip_booked.sudo().with_context(
            allow_shipment_state_write=True,
        ).write({
            'indiapost_article_number': ARTICLE,
            'indiapost_booking_state': 'booked',
            'state': 'pickup_requested',
            'indiapost_label_pdf': base64.b64encode(cls._ip_blank_pdf_bytes()),
            'indiapost_label_filename': '%s.pdf' % ARTICLE,
        })
        cls.ip_pending = cls._new_shipment(cls.ip_seller)

        cls.hub_seller = cls.env['logistics.seller'].create({
            'name': 'ARN Portal Hub Seller',
            'zip': '682001',
        })
        cls.hub_seller.write({'fulfilment_method': 'own_network'})
        cls.hub_login = 'kx_arn_hub'
        cls.env['res.users'].create({
            'name': 'ARN Portal Hub',
            'login': cls.hub_login,
            'password': cls.hub_login,
            'partner_id': cls.hub_seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        cls.hub_shipment = cls._new_shipment(cls.hub_seller)
        cls.hub_shipment.sudo().with_context(
            allow_shipment_state_write=True,
        ).write({'state': 'pickup_requested'})
        cls.hub_leftover = cls._new_shipment(cls.hub_seller)
        cls.hub_leftover.sudo().write({'indiapost_article_number': ARTICLE})

    def _assert_arn_shown(self, html, shipment):
        self.assertIn(shipment.name, html)
        self.assertIn('ARN: %s' % ARTICLE, html)

    def _assert_arn_hidden(self, html, shipment):
        self.assertIn(shipment.name, html)
        self.assertNotIn('ARN: %s' % ARTICLE, html)
        self.assertNotIn('ARN:', html)

    def test_shipments_list_shows_allocated_arn(self):
        self.authenticate(self.ip_login, self.ip_login)
        page = self.url_open('/my/shipments')
        self.assertEqual(page.status_code, 200)
        self._assert_arn_shown(page.text, self.ip_booked)
        self.assertIn(self.ip_pending.name, page.text)
        self.assertEqual(page.text.count('ARN:'), 1)

    def test_order_detail_shows_allocated_arn(self):
        self.authenticate(self.ip_login, self.ip_login)
        page = self.url_open('/my/orders/%s' % self.ip_booked.order_id.id)
        self.assertEqual(page.status_code, 200)
        self._assert_arn_shown(page.text, self.ip_booked)

        pending = self.url_open('/my/orders/%s' % self.ip_pending.order_id.id)
        self.assertEqual(pending.status_code, 200)
        self._assert_arn_hidden(pending.text, self.ip_pending)

    def test_hub_shipments_list_hides_arn(self):
        self.authenticate(self.hub_login, self.hub_login)
        page = self.url_open('/my/shipments')
        self.assertEqual(page.status_code, 200)
        self._assert_arn_hidden(page.text, self.hub_shipment)
        self._assert_arn_hidden(page.text, self.hub_leftover)
        self.assertNotIn(ARTICLE, page.text)

    def test_hub_order_detail_hides_arn(self):
        self.authenticate(self.hub_login, self.hub_login)
        page = self.url_open('/my/orders/%s' % self.hub_leftover.order_id.id)
        self.assertEqual(page.status_code, 200)
        self._assert_arn_hidden(page.text, self.hub_leftover)
        self.assertNotIn(ARTICLE, page.text)

    def _print_redirect(self, path):
        return self.url_open(path, allow_redirects=False)

    def test_seller_hides_indiapost_label_print_but_keeps_awb(self):
        self.authenticate(self.ip_login, self.ip_login)
        page = self.url_open('/my/shipments')
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(
            '/my/shipments/%s/indiapost_label' % self.ip_booked.id, page.text)
        self.assertNotIn('India Post address label', page.text)
        self.assertIn(
            '/my/shipments/%s/print' % self.ip_booked.id, page.text)

        detail = self.url_open('/my/orders/%s' % self.ip_booked.order_id.id)
        self.assertEqual(detail.status_code, 200)
        self.assertNotIn('/indiapost_label', detail.text)
        self.assertIn(
            '/my/orders/%s/print' % self.ip_booked.order_id.id, detail.text)

    def test_seller_indiapost_label_url_is_denied(self):
        self.authenticate(self.ip_login, self.ip_login)
        path = '/my/shipments/%s/indiapost_label' % self.ip_booked.id
        denied = self._print_redirect(path)
        self.assertIn(denied.status_code, (301, 302, 303, 307))
        self.assertIn('/my/shipments', denied.headers.get('Location', ''))
        self.assertNotIn('application/pdf', denied.headers.get('Content-Type', ''))

        follow = self.url_open(path)
        self.assertIn(
            'printed by KeralaXpress staff', follow.text)
        self.assertNotIn(
            'application/pdf', follow.headers.get('Content-Type', ''))
        self.assertNotIn('%PDF', follow.content[:8].decode('latin-1'))

        binary = self._print_redirect(
            '/web/content/logistics.shipment/%s/indiapost_label_pdf/%s.pdf'
            % (self.ip_booked.id, ARTICLE))
        self.assertNotIn(
            'application/pdf', binary.headers.get('Content-Type', ''))
        body = binary.content[:16] if binary.content else b''
        self.assertFalse(body.startswith(b'%PDF'))

    def test_hub_fulfilment_awb_print_when_printable(self):
        self.authenticate(self.hub_login, self.hub_login)
        self.assertTrue(self.hub_shipment.portal_awb_printable())
        page = self.url_open('/my/shipments')
        self.assertEqual(page.status_code, 200)
        self.assertIn(
            '/my/shipments/%s/print' % self.hub_shipment.id, page.text)
        self.assertNotIn('/indiapost_label', page.text)

        allowed = self._print_redirect(
            '/my/shipments/%s/print' % self.hub_shipment.id)
        self.assertIn(allowed.status_code, (301, 302, 303, 307))
        self.assertIn(
            '/report/pdf/keralariders_logistics.action_report_shipment/%s'
            % self.hub_shipment.id,
            allowed.headers.get('Location', ''),
        )
