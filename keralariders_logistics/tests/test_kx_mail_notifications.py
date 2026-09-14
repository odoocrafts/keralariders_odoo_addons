"""KeralaXpress seller/recharge emails must be queued, never SMTP-blocking.

Welcome fires on every real seller create path (admin create and partner-linked
signup-style create). Recharge request notifies the ops team; approval notifies
the seller once. ``mail.mail.send`` / ``send_after_commit`` must not run in the
request — that is what froze I Have Paid and Approve.
"""

from unittest.mock import patch

from odoo.tests import TransactionCase, tagged

ADMIN_GROUP = 'keralariders_logistics.group_logistics_admin'
OPS_PARAM = 'keralariders_logistics.ops_notification_email'


@tagged('post_install', '-at_install')
class TestKxMailNotifications(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Mail = cls.env['mail.mail']
        cls.Notify = cls.env['logistics.mail.notify']
        cls.env['ir.config_parameter'].sudo().set_param(
            'web.base.url', 'https://erp.keralaxpress.com')
        cls.env.company.sudo().write({
            'email': 'notifications@keralaxpress.com',
            'phone': '0484-0000000',
        })
        cls.admin_user = cls.env['res.users'].create({
            'name': 'KX Mail Admin',
            'login': 'kx_mail_admin',
            'email': 'kx.mail.admin@example.com',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref(ADMIN_GROUP).id,
            ])],
        })

    def setUp(self):
        super().setUp()
        self.env['ir.config_parameter'].sudo().set_param(OPS_PARAM, '')

    def _outgoing(self, subject_part=None, email_part=None):
        mails = self.Mail.sudo().search([('state', '=', 'outgoing')], order='id')
        if subject_part:
            mails = mails.filtered(
                lambda m: subject_part.lower() in (m.subject or '').lower())
        if email_part:
            mails = mails.filtered(
                lambda m: email_part.lower() in (m.email_to or '').lower())
        return mails

    def _body(self, mail):
        return '%s %s' % (mail.body_html or '', mail.body or '')

    def _assert_not_sent_in_request(self, mock_send, mock_after):
        mock_send.assert_not_called()
        mock_after.assert_not_called()

    def test_welcome_email_on_admin_seller_create(self):
        """Admin backend create of a seller with an email queues a welcome."""
        with patch.object(type(self.Mail), 'send') as mock_send, \
                patch.object(type(self.Mail), 'send_after_commit') as mock_after:
            seller = self.env['logistics.seller'].create({
                'name': 'Welcome Admin Seller',
                'email': 'welcome.admin@example.com',
                'zip': '682001',
            })
            self._assert_not_sent_in_request(mock_send, mock_after)

        mails = self._outgoing('Welcome to KeralaXpress', 'welcome.admin@example.com')
        self.assertEqual(len(mails), 1, mails.mapped('subject'))
        body = self._body(mails)
        self.assertIn('Welcome Admin Seller', body)
        self.assertIn(str(seller.id), body)
        self.assertIn('/web/login', body)
        self.assertIn('notifications@keralaxpress.com', body)

        seller.write({'phone': '9876543210'})
        self.assertEqual(
            len(self._outgoing('Welcome to KeralaXpress', 'welcome.admin@example.com')),
            1,
            'a later write must not send another welcome',
        )

    def test_welcome_email_on_partner_linked_seller_create(self):
        """Portal signup creates the user/partner first, then the seller."""
        partner = self.env['res.partner'].create({
            'name': 'Welcome Signup Seller',
            'email': 'welcome.signup@example.com',
        })
        with patch.object(type(self.Mail), 'send') as mock_send, \
                patch.object(type(self.Mail), 'send_after_commit') as mock_after:
            seller = self.env['logistics.seller'].create({
                'name': 'Welcome Signup Seller',
                'email': 'welcome.signup@example.com',
                'partner_id': partner.id,
                'zip': '682001',
            })
            self._assert_not_sent_in_request(mock_send, mock_after)

        mails = self._outgoing('Welcome to KeralaXpress', 'welcome.signup@example.com')
        self.assertEqual(len(mails), 1)
        self.assertIn(str(seller.id), self._body(mails))

    def test_no_welcome_when_seller_has_no_email(self):
        before = self._outgoing('Welcome to KeralaXpress')
        self.env['logistics.seller'].create({
            'name': 'Silent Seller',
            'zip': '682001',
        })
        after = self._outgoing('Welcome to KeralaXpress')
        self.assertEqual(before, after)

    def test_recharge_request_notifies_the_ops_team(self):
        self.env['ir.config_parameter'].sudo().set_param(
            OPS_PARAM, 'kx.ops.team@example.com')
        seller = self.env['logistics.seller'].create({
            'name': 'Recharge Mail Seller',
            'email': 'recharge.seller@example.com',
            'zip': '682001',
        })
        wallet = seller.wallet_ids[0]
        with patch.object(type(self.Mail), 'send') as mock_send, \
                patch.object(type(self.Mail), 'send_after_commit') as mock_after:
            recharge = self.env['logistics.wallet.recharge.request'].create({
                'seller_id': seller.id,
                'wallet_id': wallet.id,
                'requested_amount': 250.0,
            })
            self._assert_not_sent_in_request(mock_send, mock_after)

        mails = self._outgoing('Wallet recharge pending approval', 'kx.ops.team@example.com')
        self.assertEqual(len(mails), 1, mails.mapped('email_to'))
        body = self._body(mails)
        self.assertIn('Recharge Mail Seller', body)
        self.assertIn(recharge.name, body)
        self.assertIn('logistics.wallet.recharge.request', body)
        self.assertNotIn(
            'recharge.seller@example.com', mails.email_to or '',
            'the seller must not be the team-notify recipient',
        )

    def test_recharge_request_falls_back_to_admin_email(self):
        self.env['ir.config_parameter'].sudo().set_param(OPS_PARAM, '')
        seller = self.env['logistics.seller'].create({
            'name': 'Recharge Admin Fallback Seller',
            'email': 'recharge.fallback@example.com',
            'zip': '682001',
        })
        recharge = self.env['logistics.wallet.recharge.request'].create({
            'seller_id': seller.id,
            'wallet_id': seller.wallet_ids[0].id,
            'requested_amount': 80.0,
        })
        mails = self._outgoing('Wallet recharge pending approval')
        mails = mails.filtered(lambda m: recharge.name in (m.subject or ''))
        self.assertTrue(mails)
        combined_to = ','.join(mails.mapped('email_to'))
        self.assertIn('kx.mail.admin@example.com', combined_to)

    def test_approval_notifies_the_seller_once(self):
        self.env['ir.config_parameter'].sudo().set_param(
            OPS_PARAM, 'kx.ops.team@example.com')
        seller = self.env['logistics.seller'].create({
            'name': 'Approval Mail Seller',
            'email': 'approval.seller@example.com',
            'zip': '682001',
        })
        recharge = self.env['logistics.wallet.recharge.request'].create({
            'seller_id': seller.id,
            'wallet_id': seller.wallet_ids[0].id,
            'requested_amount': 100.0,
        })
        with patch.object(type(self.Mail), 'send') as mock_send, \
                patch.object(type(self.Mail), 'send_after_commit') as mock_after:
            recharge.action_approve_request()
            self._assert_not_sent_in_request(mock_send, mock_after)

        mails = self._outgoing('Wallet recharge approved', 'approval.seller@example.com')
        self.assertEqual(len(mails), 1)
        body = self._body(mails)
        self.assertIn(recharge.name, body)
        self.assertIn('100', body)

        recharge.action_approve_request()
        self.assertEqual(
            len(self._outgoing('Wallet recharge approved', 'approval.seller@example.com')),
            1,
            'a second approve must not send another confirmation',
        )

    def test_queue_helper_does_not_force_send(self):
        with patch.object(type(self.Mail), 'send') as mock_send, \
                patch.object(type(self.Mail), 'send_after_commit') as mock_after, \
                patch('odoo.addons.keralariders_logistics.models.mail_notify.threading.Thread') as mock_thread:
            mail = self.Notify._kx_queue_mail(
                email_to='queued@example.com',
                subject='KX queue helper',
                body_html='<p>queued</p>',
            )
            self._assert_not_sent_in_request(mock_send, mock_after)
            mock_thread.assert_not_called()
        self.assertTrue(mail)
        self.assertEqual(mail.state, 'outgoing')
        self.assertEqual(mail.email_to, 'queued@example.com')
