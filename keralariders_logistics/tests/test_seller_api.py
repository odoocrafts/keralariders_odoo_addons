"""Seller REST API: auth, seller scoping, wallet-on-pickup, idempotency."""

import json
import re

from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.keralariders_logistics.tests.common import IndiapostHermeticMixin


def _shipment_body(**overrides):
    body = {
        'customer_name': 'API Customer',
        'customer_phone': '9876543210',
        'customer_address': '12 Test Road, Test Nagar',
        'destination_pincode': '695001',
        'weight_kg': 1.5,
        'item_description': 'API parcel',
        'payment_type': 'prepaid',
        'book': True,
    }
    body.update(overrides)
    return body


@tagged('post_install', '-at_install')
class TestSellerApiCredentials(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'API Cred Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'own_network'})

    def test_secret_is_hashed_and_shown_once(self):
        Credential = self.env['logistics.seller.api.credential']
        credential, secret = Credential.generate_for_seller(self.seller)
        self.assertTrue(secret.startswith('kxs_'))
        self.assertTrue(credential.api_key.startswith('kx_'))
        self.assertNotEqual(credential.secret_hash, secret)
        self.assertFalse(Credential.search([('secret_hash', '=', secret)]))
        self.assertTrue(Credential.authenticate(credential.api_key, secret))
        self.assertFalse(Credential.authenticate(credential.api_key, 'wrong-secret'))
        self.assertFalse(Credential.authenticate(credential.api_key + 'x', secret))

    def test_regenerate_revokes_the_old_key(self):
        Credential = self.env['logistics.seller.api.credential']
        first, first_secret = Credential.generate_for_seller(self.seller)
        second, second_secret = Credential.generate_for_seller(self.seller)
        self.assertEqual(first.state, 'revoked')
        self.assertEqual(second.state, 'active')
        self.assertFalse(Credential.authenticate(first.api_key, first_secret))
        self.assertTrue(Credential.authenticate(second.api_key, second_secret))

    def test_disable_rejects_the_key(self):
        Credential = self.env['logistics.seller.api.credential']
        credential, secret = Credential.generate_for_seller(self.seller)
        credential.action_disable()
        self.assertEqual(credential.state, 'disabled')
        self.assertFalse(Credential.authenticate(credential.api_key, secret))


@tagged('post_install', '-at_install')
class TestSellerApiHttp(IndiapostHermeticMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=False)

        cls.seller = cls.env['logistics.seller'].create({
            'name': 'API HTTP Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'own_network'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.credential, cls.api_secret = cls.env['logistics.seller.api.credential'].generate_for_seller(
            cls.seller,
        )
        cls.api_key = cls.credential.api_key

        cls.other = cls.env['logistics.seller'].create({
            'name': 'API Other Seller',
            'zip': '682001',
        })
        cls.other.write({'fulfilment_method': 'own_network'})
        cls.other_cred, cls.other_secret = cls.env['logistics.seller.api.credential'].generate_for_seller(
            cls.other,
        )

        cls.portal_login = 'kx_seller_api_portal'
        cls.portal_user = cls.env['res.users'].create({
            'name': 'API Portal Seller',
            'login': cls.portal_login,
            'password': cls.portal_login,
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        cls.noseller_login = 'kx_seller_api_noseller'
        cls.env['res.users'].create({
            'name': 'API Portal Not Seller',
            'login': cls.noseller_login,
            'password': cls.noseller_login,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })

    def setUp(self):
        super().setUp()
        others = self.env['logistics.seller.api.credential'].search([
            ('seller_id', '=', self.seller.id),
            ('id', '!=', self.credential.id),
            ('state', '=', 'active'),
        ])
        if others:
            others.write({'state': 'revoked'})
        if self.credential.state != 'active':
            self.credential.state = 'active'
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': 5000.0,
            'reference': 'API test top-up',
        })
        self.wallet.invalidate_recordset(['balance'])

    def _headers(self, key=None, secret=None, idempotency=None, bearer=False):
        key = key if key is not None else self.api_key
        secret = secret if secret is not None else self.api_secret
        headers = {'Content-Type': 'application/json'}
        if bearer:
            headers['Authorization'] = 'Bearer %s:%s' % (key, secret)
        else:
            headers['X-Api-Key'] = key
            headers['X-Api-Secret'] = secret
        if idempotency:
            headers['Idempotency-Key'] = idempotency
        return headers

    def _get(self, path, **header_kw):
        return self.url_open(path, headers=self._headers(**header_kw))

    def _post(self, path, payload=None, **header_kw):
        return self.url_open(
            path,
            data=json.dumps(payload or {}).encode(),
            headers=self._headers(**header_kw),
        )

    def _json(self, response):
        return json.loads(response.text)

    def test_missing_key_is_401(self):
        response = self.url_open(
            '/api/v1/seller/wallet',
            headers={'Content-Type': 'application/json'},
        )
        self.assertEqual(response.status_code, 401)
        body = self._json(response)
        self.assertFalse(body['ok'])
        self.assertEqual(body['error']['code'], 'unauthorized')

    def test_invalid_secret_is_401(self):
        response = self._get('/api/v1/seller/wallet', secret='kxs_not-the-secret')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self._json(response)['error']['code'], 'unauthorized')

    def test_disabled_key_is_401(self):
        credential, secret = self.env['logistics.seller.api.credential'].generate_for_seller(
            self.seller, name='To disable',
        )
        # Restore the fixture key as the active one after generate revoked it.
        self.credential.state = 'active'
        credential.action_disable()
        response = self._get(
            '/api/v1/seller/wallet', key=credential.api_key, secret=secret,
        )
        self.assertEqual(response.status_code, 401)

    def test_wallet_balance_matches_the_seller(self):
        response = self._get('/api/v1/seller/wallet')
        self.assertEqual(response.status_code, 200)
        body = self._json(response)
        self.assertTrue(body['ok'])
        self.assertAlmostEqual(body['data']['balance'], self.wallet.balance, places=2)
        self.assertEqual(body['data']['currency'], 'INR')

    def test_bearer_header_also_authenticates(self):
        response = self._get('/api/v1/seller/wallet', bearer=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self._json(response)['ok'])

    def test_rates_use_the_existing_calculator(self):
        response = self._post('/api/v1/seller/rates', {
            'origin_pincode': '682001',
            'destination_pincode': '695001',
            'weight_kg': 1.5,
        })
        self.assertEqual(response.status_code, 200)
        body = self._json(response)
        self.assertTrue(body['ok'])
        expected = self.env['logistics.delivery.charges'].calculate_delivery_charge(
            1.5, False, package_id=self.seller.delivery_package_id.id or None,
        )
        self.assertAlmostEqual(body['data']['charge'], expected, places=2)
        self.assertEqual(body['data']['method'], 'own_network')

    def test_pincode_serviceability(self):
        response = self._get('/api/v1/seller/pincodes/695001')
        self.assertEqual(response.status_code, 200)
        body = self._json(response)
        self.assertTrue(body['ok'])
        self.assertTrue(body['data']['serviceable'])
        self.assertEqual(body['data']['pincode'], '695001')

        unknown = self._get('/api/v1/seller/pincodes/000000')
        self.assertEqual(unknown.status_code, 200)
        self.assertFalse(self._json(unknown)['data']['serviceable'])

    def test_create_books_and_debits_wallet(self):
        opening = self.wallet.balance
        response = self._post(
            '/api/v1/seller/shipments',
            _shipment_body(),
            idempotency='create-debit-1',
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = self._json(response)
        self.assertTrue(body['ok'])
        data = body['data']
        self.assertTrue(data['awb'])
        self.assertEqual(data['state'], 'pickup_requested')
        self.assertTrue(data['wallet_debited'])
        self.assertGreater(data['delivery_charge'], 1.0)

        self.env.invalidate_all()
        self.wallet.invalidate_recordset(['balance'])
        shipment = self.env['logistics.shipment'].search([
            ('name', '=', data['awb']),
            ('seller_id', '=', self.seller.id),
        ], limit=1)
        self.assertTrue(shipment)
        self.assertTrue(shipment.wallet_transaction_id)
        self.assertEqual(shipment.state, 'pickup_requested')
        self.assertAlmostEqual(
            self.wallet.balance, opening - shipment.delivery_charges_total, places=2,
        )

    def test_create_rejects_insufficient_wallet_without_leaving_a_draft(self):
        broke = self.env['logistics.seller'].create({
            'name': 'API Broke Seller',
            'zip': '682001',
        })
        broke.write({'fulfilment_method': 'own_network'})
        cred, secret = self.env['logistics.seller.api.credential'].generate_for_seller(broke)
        before = self.env['logistics.shipment'].search_count([
            ('seller_id', '=', broke.id),
        ])
        response = self._post(
            '/api/v1/seller/shipments',
            _shipment_body(),
            key=cred.api_key,
            secret=secret,
        )
        self.assertEqual(response.status_code, 402, response.text)
        self.assertEqual(self._json(response)['error']['code'], 'insufficient_wallet')
        self.env.invalidate_all()
        self.assertEqual(
            self.env['logistics.shipment'].search_count([
                ('seller_id', '=', broke.id),
            ]),
            before,
        )

    def test_create_ignores_injected_charge_and_fulfilment_fields(self):
        response = self._post('/api/v1/seller/shipments', _shipment_body(
            fulfilment_method='indiapost',
            delivery_charges_total=1.0,
            delivery_charges_subtotal=1.0,
            tax_percentage=-1,
            book=False,
        ))
        self.assertIn(response.status_code, (200, 201), response.text)
        body = self._json(response)
        shipment = self.env['logistics.shipment'].search([
            ('name', '=', body['data']['awb']),
        ], limit=1)
        self.assertEqual(shipment.fulfilment_method, 'own_network')
        expected = self.env['logistics.delivery.charges'].calculate_delivery_charge(
            1.5,
            shipment.shipping_from_district_id == shipment.shipping_to_district_id,
            package_id=self.seller.delivery_package_id.id or None,
        )
        self.assertAlmostEqual(shipment.delivery_charges_total, expected, places=2)
        self.assertNotAlmostEqual(shipment.delivery_charges_total, 1.0, places=2)
        self.assertEqual(shipment.state, 'order_added')
        self.assertFalse(shipment.wallet_transaction_id)

    def test_cod_fields_are_preserved(self):
        response = self._post('/api/v1/seller/shipments', _shipment_body(
            payment_type='cod',
            cod_amount=1500,
            book=False,
        ))
        self.assertIn(response.status_code, (200, 201), response.text)
        shipment = self.env['logistics.shipment'].search([
            ('name', '=', self._json(response)['data']['awb']),
        ], limit=1)
        self.assertEqual(shipment.order_payment_type, 'cod')
        self.assertAlmostEqual(shipment.cod_amount, 1500.0, places=2)

    def test_idempotency_key_does_not_duplicate_awb(self):
        first = self._post(
            '/api/v1/seller/shipments',
            _shipment_body(),
            idempotency='shop-order-99',
        )
        self.assertEqual(first.status_code, 201, first.text)
        awb = self._json(first)['data']['awb']
        second = self._post(
            '/api/v1/seller/shipments',
            _shipment_body(),
            idempotency='shop-order-99',
        )
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(self._json(second)['data']['awb'], awb)
        self.assertEqual(
            self.env['logistics.shipment'].search_count([
                ('seller_id', '=', self.seller.id),
                ('name', '=', awb),
            ]),
            1,
        )

    def test_idempotency_key_rejects_a_different_body(self):
        self._post(
            '/api/v1/seller/shipments',
            _shipment_body(),
            idempotency='shop-order-conflict',
        )
        conflict = self._post(
            '/api/v1/seller/shipments',
            _shipment_body(customer_name='Someone Else'),
            idempotency='shop-order-conflict',
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(self._json(conflict)['error']['code'], 'conflict')

    def test_seller_cannot_read_another_sellers_awb(self):
        created = self._post('/api/v1/seller/shipments', _shipment_body(book=False))
        awb = self._json(created)['data']['awb']
        other = self._get(
            '/api/v1/seller/shipments/%s' % awb,
            key=self.other_cred.api_key,
            secret=self.other_secret,
        )
        self.assertEqual(other.status_code, 404)
        self.assertEqual(self._json(other)['error']['code'], 'not_found')

        own = self._get('/api/v1/seller/shipments/%s' % awb)
        self.assertEqual(own.status_code, 200)
        self.assertEqual(self._json(own)['data']['awb'], awb)

    def test_list_is_scoped_to_the_authenticated_seller(self):
        mine = self._post('/api/v1/seller/shipments', _shipment_body(book=False))
        awb = self._json(mine)['data']['awb']
        listing = self._get(
            '/api/v1/seller/shipments',
            key=self.other_cred.api_key,
            secret=self.other_secret,
        )
        self.assertEqual(listing.status_code, 200)
        awbs = [row['awb'] for row in self._json(listing)['data']['shipments']]
        self.assertNotIn(awb, awbs)

    def test_track_matches_public_wording_and_omits_wallet(self):
        created = self._post('/api/v1/seller/shipments', _shipment_body())
        awb = self._json(created)['data']['awb']
        tracked = self._get('/api/v1/seller/shipments/%s/track' % awb)
        self.assertEqual(tracked.status_code, 200)
        payload = json.dumps(self._json(tracked)).lower()
        self.assertNotIn('wallet', payload)
        self.assertNotIn('free return', payload)
        self.assertNotIn('no charge', payload)
        self.assertIn('pickup requested', payload)

    def test_return_does_not_debit_wallet(self):
        created = self._post('/api/v1/seller/shipments', _shipment_body(book=False))
        awb = self._json(created)['data']['awb']
        shipment = self.env['logistics.shipment'].search([('name', '=', awb)], limit=1)
        shipment.sudo().with_context(allow_shipment_state_write=True).write({
            'state': 'delivered',
        })
        self.env.invalidate_all()
        opening = self.wallet.balance
        response = self._post('/api/v1/seller/shipments/%s/return' % awb)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(self._json(response)['data']['wallet_debited'])
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, opening, places=2)
        self.assertTrue(shipment.is_return_journey)

    def test_portal_api_page_is_seller_only(self):
        self.authenticate(self.portal_login, self.portal_login)
        page = self.url_open('/my/api')
        self.assertEqual(page.status_code, 200)
        self.assertIn('/api/v1/seller', page.text)
        self.assertIn('X-Api-Key', page.text)
        self.assertIn(self.api_key, page.text)
        self.assertNotIn(self.api_secret, page.text)

        self.authenticate(self.noseller_login, self.noseller_login)
        denied = self.url_open('/my/api')
        self.assertNotIn('X-Api-Key', denied.text)
        self.assertNotIn('Generate API key', denied.text)

    def test_portal_generate_shows_the_secret_once(self):
        self.authenticate(self.portal_login, self.portal_login)
        page = self.url_open('/my/api')
        match = re.search(r'name="csrf_token"[^>]*\bvalue="([^"]*)"', page.text)
        self.assertTrue(match)
        generated = self.url_open('/my/api/keys/generate', data={
            'csrf_token': match.group(1),
        })
        self.assertEqual(generated.status_code, 200)
        self.assertIn('Copy your API secret now', generated.text)
        self.assertIn('kxs_', generated.text)
        self.env.invalidate_all()
        # Reloading the docs page must not show the secret again.
        again = self.url_open('/my/api')
        self.assertNotIn('Copy your API secret now', again.text)
        self.assertNotIn('kxs_', again.text)
