"""Two India Post contracts, one per product.

India Post contracts a bulk customer per service, so KeralaXpress holds a Speed
Post contract and a Business Parcel contract. Booking is validated against the
contract belonging to the article's own product, which makes "which contract"
a per-shipment decision rather than a single setting.

No API calls: everything here is payload assembly and configuration.
"""

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc

SP_CONTRACT = '41124829'
BP_CONTRACT = '41664688'
PREFIX = 'keralariders_logistics.'


@tagged('post_install', '-at_install')
class TestIndiapostContracts(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'India Post Contract Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Kochi Head Office',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})

    def setUp(self):
        super().setUp()
        self.Client = self.env['logistics.indiapost.client']
        self.params = self.env['ir.config_parameter'].sudo()
        for key, value in (
            ('indiapost_enabled', 'True'),
            ('indiapost_username', '9999537187'),
            ('indiapost_password', 'secret'),
            ('indiapost_customer_id', '9999537187'),
            ('indiapost_sp_contract_id', SP_CONTRACT),
            ('indiapost_bp_contract_id', BP_CONTRACT),
            ('indiapost_sender_name', 'KERALA XPRESS LOGISTICS'),
            ('indiapost_sender_company', 'KERALA XPRESS LOGISTICS'),
            ('indiapost_sender_address', 'Vazhiyambalam, Bypass NH66'),
            ('indiapost_sender_city', 'Thrissur'),
            ('indiapost_sender_state', 'Kerala'),
            ('indiapost_sender_pincode', '680681'),
            ('indiapost_sender_mobile', '9400662693'),
        ):
            self.params.set_param(PREFIX + key, value)
        self.settings = self.Client._ip_settings()

    def _new_shipment(self, **overrides):
        vals = {
            'seller_id': self.seller.id,
            'shipping_to_name': 'India Post Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.5,
            'length_cm': 30,
            'breadth_cm': 20,
            'height_cm': 15,
        }
        vals.update(overrides)
        return self.env['logistics.shipment'].create(vals)

    # ------------------------------------------------------------------
    # The right contract for the product
    # ------------------------------------------------------------------
    def test_each_product_resolves_its_own_contract(self):
        self.assertEqual(
            self.Client._ip_contract_id(self.settings,
                                        ipc.ARTICLE_TYPE_SPEED_POST),
            SP_CONTRACT,
        )
        self.assertEqual(
            self.Client._ip_contract_id(self.settings,
                                        ipc.ARTICLE_TYPE_BUSINESS_PARCEL),
            BP_CONTRACT,
        )

    def test_a_shipment_defaults_to_speed_post(self):
        shipment = self._new_shipment()
        self.assertEqual(shipment.indiapost_article_type,
                         ipc.ARTICLE_TYPE_SPEED_POST)
        self.assertEqual(shipment._ip_contract_id(self.settings), SP_CONTRACT)

    def test_the_booking_payload_carries_the_contract_of_its_product(self):
        """The point of the whole change, at the place it has to hold."""
        speed_post = self._new_shipment()
        parcel = self._new_shipment(
            indiapost_article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL)

        sp_article = speed_post._ip_prepare_article(self.settings,
                                                    'ET214330016IN')
        bp_article = parcel._ip_prepare_article(self.settings, 'ET214330024IN')

        self.assertEqual(sp_article['article_type'],
                         ipc.ARTICLE_TYPE_SPEED_POST)
        self.assertEqual(sp_article['contract_id'], SP_CONTRACT)
        self.assertEqual(bp_article['article_type'],
                         ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(bp_article['contract_id'], BP_CONTRACT)

    def test_the_label_prints_the_product_it_was_booked_as(self):
        parcel = self._new_shipment(
            indiapost_article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        parcel.sudo().write({'indiapost_article_number': 'ET214330016IN'})
        payload = parcel._ip_label_payload(self.settings)
        self.assertEqual(payload['service_type'],
                         ipc.ARTICLE_TYPE_BUSINESS_PARCEL)

    def test_the_product_is_part_of_the_quote_fingerprint(self):
        """A product change has to invalidate a stored rate, like a reweigh."""
        shipment = self._new_shipment()
        speed_post_signature = shipment._ip_quote_signature()
        shipment.indiapost_article_type = ipc.ARTICLE_TYPE_BUSINESS_PARCEL
        self.assertNotEqual(shipment._ip_quote_signature(),
                            speed_post_signature)

    def test_the_tariff_request_asks_for_the_right_product(self):
        Tariff = self.env['logistics.indiapost.tariff']
        params = Tariff._ip_tariff_params(
            '682001', '110001', 250, 30, 21, 2,
            article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(params['product-code'],
                         ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        # Two products at the same size are two different prices, so they may
        # not share a cache entry.
        args = ('682001', '110001', 250, 30, 21, 2, 'none', 'sandbox')
        self.assertNotEqual(
            Tariff._ip_cache_key(*args,
                                 article_type=ipc.ARTICLE_TYPE_SPEED_POST),
            Tariff._ip_cache_key(
                *args, article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL),
        )

    # ------------------------------------------------------------------
    # A contract that is not configured
    # ------------------------------------------------------------------
    def test_a_missing_contract_says_which_one_is_missing(self):
        self.params.set_param(PREFIX + 'indiapost_bp_contract_id', '')
        settings = self.Client._ip_settings()

        with self.assertRaises(UserError) as caught:
            self.Client._ip_contract_id(settings,
                                        ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertIn('Business Parcel', str(caught.exception))

        # And the product that *is* configured still books.
        self.assertEqual(
            self.Client._ip_contract_id(settings,
                                        ipc.ARTICLE_TYPE_SPEED_POST),
            SP_CONTRACT,
        )

    def test_a_malformed_contract_is_refused_before_it_is_sent(self):
        self.params.set_param(PREFIX + 'indiapost_sp_contract_id', '4112')
        settings = self.Client._ip_settings()
        with self.assertRaises(UserError) as caught:
            self.Client._ip_contract_id(settings, ipc.ARTICLE_TYPE_SPEED_POST)
        message = str(caught.exception)
        self.assertIn('Speed Post', message)
        self.assertIn('8 digits', message)

    def test_an_unknown_product_has_no_contract(self):
        with self.assertRaises(UserError):
            self.Client._ip_contract_id(self.settings, 'PP')

    def test_only_the_contracts_this_batch_needs_are_required(self):
        """A Speed Post run is not blocked by a blank Business Parcel contract."""
        self.params.set_param(PREFIX + 'indiapost_bp_contract_id', '')
        settings = self.Client._ip_settings()
        speed_post = self._new_shipment()
        speed_post._ip_check_booking_settings(settings)

        parcel = self._new_shipment(
            indiapost_article_type=ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        with self.assertRaises(UserError) as caught:
            (speed_post | parcel)._ip_check_booking_settings(settings)
        self.assertIn('Business Parcel', str(caught.exception))

    # ------------------------------------------------------------------
    # Settings screen
    # ------------------------------------------------------------------
    def test_both_contracts_survive_a_settings_round_trip(self):
        Settings = self.env['res.config.settings']
        Settings.create({
            'indiapost_sp_contract_id': SP_CONTRACT,
            'indiapost_bp_contract_id': BP_CONTRACT,
        }).execute()

        self.assertEqual(
            self.params.get_param(PREFIX + 'indiapost_sp_contract_id'),
            SP_CONTRACT)
        self.assertEqual(
            self.params.get_param(PREFIX + 'indiapost_bp_contract_id'),
            BP_CONTRACT)

        defaults = Settings.default_get(['indiapost_sp_contract_id',
                                         'indiapost_bp_contract_id'])
        self.assertEqual(defaults.get('indiapost_sp_contract_id'), SP_CONTRACT)
        self.assertEqual(defaults.get('indiapost_bp_contract_id'), BP_CONTRACT)

        settings = self.Client._ip_settings()
        self.assertEqual(settings['indiapost_sp_contract_id'], SP_CONTRACT)
        self.assertEqual(settings['indiapost_bp_contract_id'], BP_CONTRACT)

    def test_the_settings_screen_rejects_a_contract_of_the_wrong_length(self):
        for field_name, product in (('indiapost_sp_contract_id', 'Speed Post'),
                                    ('indiapost_bp_contract_id',
                                     'Business Parcel')):
            with self.assertRaises(ValidationError) as caught:
                self.env['res.config.settings'].create({
                    'indiapost_enabled': True,
                    'indiapost_username': '9999537187',
                    'indiapost_password': 'secret',
                    'indiapost_customer_id': '9999537187',
                    'indiapost_sender_name': 'KERALA XPRESS LOGISTICS',
                    'indiapost_sender_company': 'KERALA XPRESS LOGISTICS',
                    'indiapost_sender_address': 'Vazhiyambalam, Bypass NH66',
                    'indiapost_sender_city': 'Thrissur',
                    'indiapost_sender_state': 'Kerala',
                    'indiapost_sender_pincode': '680681',
                    'indiapost_sender_mobile': '9400662693',
                    field_name: '1234567',
                }).execute()
            self.assertIn(product, str(caught.exception))
