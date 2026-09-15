"""Seller portal: update own COD bank details, never another seller's."""

import re

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, tagged


def _csrf(html):
    match = re.search(r'name="csrf_token"[^>]*\bvalue="([^"]*)"', html)
    return match.group(1) if match else None


@tagged('post_install', '-at_install')
class TestSellerBankVals(TransactionCase):

    def test_prepare_cod_bank_vals_requires_name_number_ifsc(self):
        Seller = self.env['logistics.seller']
        with self.assertRaises(UserError):
            Seller.prepare_cod_bank_vals({
                'bank_account_name': '',
                'bank_account_number': '123456789012',
                'bank_ifsc': 'SBIN0001234',
            })
        with self.assertRaises(UserError):
            Seller.prepare_cod_bank_vals({
                'bank_account_name': 'Holder',
                'bank_account_number': '',
                'bank_ifsc': 'SBIN0001234',
            })
        with self.assertRaises(UserError):
            Seller.prepare_cod_bank_vals({
                'bank_account_name': 'Holder',
                'bank_account_number': '123456789012',
                'bank_ifsc': '',
            })

    def test_prepare_cod_bank_vals_rejects_invalid_ifsc(self):
        with self.assertRaises(UserError):
            self.env['logistics.seller'].prepare_cod_bank_vals({
                'bank_account_name': 'Holder',
                'bank_account_number': '123456789012',
                'bank_ifsc': 'FOO',
            })
        with self.assertRaises(UserError):
            self.env['logistics.seller'].prepare_cod_bank_vals({
                'bank_account_name': 'Holder',
                'bank_account_number': '123456789012',
                'bank_ifsc': 'sbin0001234x',
            })

    def test_prepare_cod_bank_vals_normalises_ifsc_and_spaces(self):
        vals = self.env['logistics.seller'].prepare_cod_bank_vals({
            'bank_account_name': '  Anas Holder  ',
            'bank_account_number': '1234 5678 9012',
            'bank_ifsc': 'sbin 0001234',
            'bank_name': ' State Bank ',
            'bank_branch': ' Kochi ',
        })
        self.assertEqual(vals['bank_account_name'], 'Anas Holder')
        self.assertEqual(vals['bank_account_number'], '123456789012')
        self.assertEqual(vals['bank_ifsc'], 'SBIN0001234')
        self.assertEqual(vals['bank_name'], 'State Bank')
        self.assertEqual(vals['bank_branch'], 'Kochi')


@tagged('post_install', '-at_install')
class TestPortalBankDetails(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Bank Portal Seller',
            'zip': '682001',
            'bank_account_name': 'Bank Portal Seller',
            'bank_account_number': '111122223333',
            'bank_ifsc': 'SBIN0001111',
            'bank_name': 'State Bank of India',
            'bank_branch': 'Ernakulam',
        })
        cls.seller.write({'fulfilment_method': 'own_network'})
        cls.portal_login = 'kx_bank_portal'
        cls.portal_user = cls.env['res.users'].create({
            'name': 'Bank Portal Seller',
            'login': cls.portal_login,
            'password': cls.portal_login,
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

        cls.other = cls.env['logistics.seller'].create({
            'name': 'Bank Other Seller',
            'zip': '682001',
            'bank_account_name': 'Other Holder',
            'bank_account_number': '999988887777',
            'bank_ifsc': 'HDFC0009999',
            'bank_name': 'HDFC Bank',
            'bank_branch': 'Thrissur',
        })
        cls.other.write({'fulfilment_method': 'own_network'})
        cls.other_login = 'kx_bank_other'
        cls.env['res.users'].create({
            'name': 'Bank Other Seller',
            'login': cls.other_login,
            'password': cls.other_login,
            'partner_id': cls.other.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

        cls.noseller_login = 'kx_bank_noseller'
        cls.env['res.users'].create({
            'name': 'Bank Portal Not Seller',
            'login': cls.noseller_login,
            'password': cls.noseller_login,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def test_portal_user_cannot_write_bank_fields_via_orm(self):
        with self.assertRaises(AccessError):
            self.seller.with_user(self.portal_user).write({
                'bank_ifsc': 'HDFC0001234',
            })

    def test_seller_sees_own_prefilled_bank_form(self):
        self.authenticate(self.portal_login, self.portal_login)
        home = self.url_open('/my')
        self.assertEqual(home.status_code, 200)
        self.assertIn('/my/bank', home.text)
        self.assertIn('Bank Account', home.text)
        self.assertIn('Update account used for COD settlements', home.text)

        page = self.url_open('/my/bank')
        self.assertEqual(page.status_code, 200)
        self.assertIn('action="/my/bank/update"', page.text)
        self.assertIn('111122223333', page.text)
        self.assertIn('SBIN0001111', page.text)
        self.assertIn('Bank Portal Seller', page.text)
        self.assertNotIn('999988887777', page.text)
        self.assertNotIn('HDFC0009999', page.text)

    def test_seller_can_update_own_bank_details(self):
        self.authenticate(self.portal_login, self.portal_login)
        page = self.url_open('/my/bank')
        token = _csrf(page.text)
        self.assertTrue(token)
        saved = self.url_open('/my/bank/update', data={
            'csrf_token': token,
            'bank_account_name': 'Cheeran Toys Pvt LTD',
            'bank_account_number': '555566667777',
            'bank_ifsc': 'sbin0004321',
            'bank_name': 'Canara Bank',
            'bank_branch': 'Wadakkanchery',
        })
        self.assertEqual(saved.status_code, 200)
        self.assertIn('Bank details updated', saved.text)
        self.assertIn('555566667777', saved.text)
        self.assertIn('SBIN0004321', saved.text)
        self.env.invalidate_all()
        self.assertEqual(self.seller.bank_account_name, 'Cheeran Toys Pvt LTD')
        self.assertEqual(self.seller.bank_account_number, '555566667777')
        self.assertEqual(self.seller.bank_ifsc, 'SBIN0004321')
        self.assertEqual(self.seller.bank_name, 'Canara Bank')
        self.assertEqual(self.seller.bank_branch, 'Wadakkanchery')
        # Other seller untouched.
        self.assertEqual(self.other.bank_account_number, '999988887777')
        self.assertEqual(self.other.bank_ifsc, 'HDFC0009999')

    def test_seller_cannot_overwrite_another_seller_by_posting_ids(self):
        self.authenticate(self.portal_login, self.portal_login)
        page = self.url_open('/my/bank')
        token = _csrf(page.text)
        self.url_open('/my/bank/update', data={
            'csrf_token': token,
            'seller_id': str(self.other.id),
            'id': str(self.other.id),
            'bank_account_name': 'Hijacked',
            'bank_account_number': '000011112222',
            'bank_ifsc': 'SBIN0000001',
            'bank_name': 'Hijack Bank',
            'bank_branch': 'Nowhere',
        })
        self.env.invalidate_all()
        self.assertEqual(self.seller.bank_account_name, 'Hijacked')
        self.assertEqual(self.seller.bank_account_number, '000011112222')
        self.assertEqual(self.other.bank_account_name, 'Other Holder')
        self.assertEqual(self.other.bank_account_number, '999988887777')
        self.assertEqual(self.other.bank_ifsc, 'HDFC0009999')

    def test_other_seller_does_not_see_updated_details(self):
        self.seller.write({
            'bank_account_name': 'Secret Holder',
            'bank_account_number': '444455556666',
            'bank_ifsc': 'CNRB0004444',
        })
        self.authenticate(self.other_login, self.other_login)
        page = self.url_open('/my/bank')
        self.assertEqual(page.status_code, 200)
        self.assertIn('999988887777', page.text)
        self.assertNotIn('444455556666', page.text)
        self.assertNotIn('Secret Holder', page.text)
        self.assertNotIn('CNRB0004444', page.text)

    def test_validation_error_does_not_write(self):
        self.authenticate(self.portal_login, self.portal_login)
        page = self.url_open('/my/bank')
        token = _csrf(page.text)
        before_number = self.seller.bank_account_number
        before_ifsc = self.seller.bank_ifsc
        failed = self.url_open('/my/bank/update', data={
            'csrf_token': token,
            'bank_account_name': 'Holder',
            'bank_account_number': '123456789012',
            'bank_ifsc': 'NOTANIFSC',
            'bank_name': 'Nope',
            'bank_branch': '',
        })
        self.assertEqual(failed.status_code, 200)
        self.assertIn('IFSC must be 11 characters', failed.text)
        self.env.invalidate_all()
        self.assertEqual(self.seller.bank_account_number, before_number)
        self.assertEqual(self.seller.bank_ifsc, before_ifsc)

        missing = self.url_open('/my/bank/update', data={
            'csrf_token': token,
            'bank_account_name': '',
            'bank_account_number': '',
            'bank_ifsc': '',
        })
        self.assertIn('required for COD settlements', missing.text)
        self.env.invalidate_all()
        self.assertEqual(self.seller.bank_account_number, before_number)

    def test_non_seller_is_redirected(self):
        self.authenticate(self.noseller_login, self.noseller_login)
        home = self.url_open('/my')
        self.assertNotIn('Update account used for COD settlements', home.text)
        denied = self.url_open('/my/bank')
        self.assertNotIn('action="/my/bank/update"', denied.text)
        self.assertNotIn('Save Bank Details', denied.text)
        token = _csrf(home.text) or _csrf(denied.text)
        if token:
            posted = self.url_open('/my/bank/update', data={
                'csrf_token': token,
                'bank_account_name': 'Nope',
                'bank_account_number': '123456789012',
                'bank_ifsc': 'SBIN0001234',
            })
            self.assertNotIn('Bank details updated', posted.text)
        self.env.invalidate_all()
        self.assertEqual(self.seller.bank_account_number, '111122223333')
        self.assertEqual(self.other.bank_account_number, '999988887777')
