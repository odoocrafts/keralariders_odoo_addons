"""Where the delivery charge guard meets the India Post tariff.

The guard on ``logistics.shipment`` prices a shipment again from the rate card
immediately before the wallet is debited, so that a tampered charge column
cannot decide what a seller pays. An India Post shipment is not priced from the
rate card at all — it is billed the postal tariff — so the two have to agree on
where the authoritative number comes from, or every Speed Post parcel would be
debited at hub-network rates.

The other half of the interaction is staleness. The India Post work already
refuses to debit against a quote whose signature no longer describes the
article; a manual price set by ops has to survive that without letting a stale
tariff through, and it does because the override carries the quote signature.

Quotes that the tests need are stored the way ``_ip_quote_and_store``
would. The HTTP client is patched so a debit that is forced to re-quote
cannot reach India Post, even on a production copy of the database.
"""

from odoo import fields as odoo_fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc
from odoo.addons.keralariders_logistics.tests.common import (
    IP_NETWORK_BLOCKED,
    IndiapostHermeticMixin,
)


@tagged('post_install', '-at_install')
class TestIndiapostDeliveryCharge(IndiapostHermeticMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'India Post Charge Seller',
            'zip': '682001',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.portal_user = cls.env['res.users'].create({
            'name': 'India Post Charge Portal',
            'login': 'kx_ip_charge_portal',
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
        self.shipment = self._new_shipment()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _new_shipment(self, **overrides):
        """A parcel India Post accepts: 1.5 kg at 30 x 20 x 15 cm."""
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

    def _store_quote(self, shipment=None, base=100.0, vas=0.0, tax=18.0,
                     total=118.0):
        """Exactly what _ip_quote_and_store writes, without calling the API."""
        shipment = shipment or self.shipment
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

    def _slab_price(self, shipment=None):
        shipment = shipment or self.shipment
        return self.env['logistics.delivery.charges'].calculate_delivery_charge(
            shipment.total_weight,
            shipment.shipping_from_district_id == shipment.shipping_to_district_id,
            package_id=None,
        )

    # ------------------------------------------------------------------
    # The recompute has to price the right way round
    # ------------------------------------------------------------------
    def test_the_debit_uses_the_postal_tariff_not_the_rate_card(self):
        """The crux of composing the two changes.

        Layer 2 recomputes the charge before debiting. If it recomputed the
        slab price, this parcel would be billed the hub-network rate instead of
        the postal one — cheaper here, and wrong either way.
        """
        self._store_quote(total=118.0)
        slab = self._slab_price()
        self.assertNotAlmostEqual(slab, 118.0,
                                  msg='the fixture must tell the two apart')

        self.shipment.action_add_wallet_transaction()
        self.assertAlmostEqual(self.shipment.wallet_transaction_id.amount,
                               -118.0, places=2)

    def test_an_own_network_shipment_is_still_billed_the_slab_price(self):
        """The other branch of the same dispatch."""
        self.seller.write({'fulfilment_method': 'own_network'})
        own = self._new_shipment()
        self.assertEqual(own.fulfilment_method, 'own_network')

        own.action_add_wallet_transaction()
        self.assertAlmostEqual(own.wallet_transaction_id.amount,
                               -self._slab_price(own), places=2)
        self.seller.write({'fulfilment_method': 'indiapost'})

    # ------------------------------------------------------------------
    # The tariff is what the seller pays, so it is staff-only
    # ------------------------------------------------------------------
    def test_portal_cannot_write_the_postal_tariff(self):
        """``indiapost_total_tariff = 1`` was the same hole, one field along."""
        self._store_quote(total=118.0)
        for field in ('indiapost_base_tariff', 'indiapost_vas_charges',
                      'indiapost_tax_amount', 'indiapost_total_tariff'):
            with self.assertRaises(AccessError, msg=field):
                self.shipment.with_user(self.portal_user).sudo().write(
                    {field: 1.0})
        self.assertAlmostEqual(self.shipment.delivery_charges_total, 118.0,
                               places=2)

    def test_portal_cannot_backdate_the_quote_signature(self):
        """Faking freshness would walk straight past the re-quote guard.

        The signature and the quote timestamp are what decide whether the
        stored tariff is trusted, so they are as sensitive as the amounts.
        """
        with self.assertRaises(AccessError):
            self.shipment.with_user(self.portal_user).sudo().write({
                'indiapost_quote_signature': self.shipment._ip_quote_signature(),
                'indiapost_tariff_quoted_on': odoo_fields.Datetime.now(),
            })
        self.assertTrue(self.shipment.indiapost_needs_quote)

    def test_sellers_keep_declaring_the_article_itself(self):
        """Insurance and VAS move the tariff but stay the seller's to declare.

        They are safe to leave open because they are inputs to the quote, not
        the quote: changing one invalidates the signature and forces a fresh
        tariff rather than lowering the price.
        """
        self._store_quote()
        self.assertFalse(self.shipment.indiapost_needs_quote)

        self.shipment.with_user(self.portal_user).sudo().write({
            'indiapost_insurance_value': 50000.0,
            'indiapost_vas_pod': True,
        })
        self.assertTrue(self.shipment.indiapost_needs_quote,
                        'a declared value change did not invalidate the quote')

    # ------------------------------------------------------------------
    # Manual price versus the re-quote
    # ------------------------------------------------------------------
    def test_an_unquoted_india_post_shipment_cannot_be_debited(self):
        """Unchanged behaviour, and the contrast for the next test.

        India Post is enabled with stub credentials and the HTTP client is
        blocked, so the forced re-quote fails. That failure has to stop the
        debit rather than charging an unquoted parcel, and it must not depend
        on the integration happening to be switched off.
        """
        self.assertTrue(self.shipment.indiapost_needs_quote)
        with self.assertRaises(UserError) as caught:
            self.shipment.action_add_wallet_transaction()
        message = str(caught.exception)
        self.assertIn('out of date', message)
        self.assertIn(IP_NETWORK_BLOCKED, message)
        self.assertFalse(self.shipment.wallet_transaction_id)

    def test_an_ops_override_is_billed_without_re_quoting(self):
        """The ops escape hatch has to work when India Post does not.

        Pricing an odd parcel by hand is exactly what ops do when the article
        cannot be quoted, so an override must not be held up waiting for a
        tariff that is no longer what the seller is being billed.
        """
        self.assertTrue(self.shipment.indiapost_needs_quote)
        self.shipment.write({
            'delivery_charges_subtotal': 150.0,
            'delivery_charges_total': 150.0,
        })
        self.assertTrue(self.shipment._delivery_charge_override_applies())

        self.shipment.action_add_wallet_transaction()
        self.assertAlmostEqual(self.shipment.wallet_transaction_id.amount,
                               -150.0, places=2)

    def test_an_override_lapses_on_a_change_only_india_post_prices(self):
        """The override and the quote go stale on the same events.

        A dimension change leaves the weight band alone but moves the postal
        tariff (volumetric weight), so the slab-only fingerprint would have
        kept honouring a price for a parcel that no longer exists.
        """
        self._store_quote()
        self.shipment.write({
            'delivery_charges_subtotal': 150.0,
            'delivery_charges_total': 150.0,
        })
        self.assertTrue(self.shipment._delivery_charge_override_applies())
        banded = self.shipment.indiapost_quoted_weight_g

        self.shipment.with_user(self.portal_user).sudo().write({'height_cm': 25})
        self.assertEqual(
            ipc.band_weight(ipc.kg_to_grams(self.shipment.total_weight)), banded,
            'the weight band moved, so this case proves nothing')
        self.assertFalse(self.shipment._delivery_charge_override_applies())

        # ...and with the override gone, the stale quote stops the debit.
        # The client is blocked, so this is the business rule, not "India
        # Post happens to be off" and not a live re-quote.
        with self.assertRaises(UserError) as caught:
            self.shipment.action_add_wallet_transaction()
        message = str(caught.exception)
        self.assertIn('out of date', message)
        self.assertIn(IP_NETWORK_BLOCKED, message)
        self.assertFalse(self.shipment.wallet_transaction_id)


@tagged('post_install', '-at_install')
class TestIndiapostSpeedPostParcelMapping(IndiapostHermeticMixin, TransactionCase):
    """Speed Post at 500 g+ stays inland parcel; below 500 g becomes BP."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ip_make_hermetic(enabled=True)
        cls.seller = cls.env['logistics.seller'].create({
            'name': 'India Post Parcel Mapping Seller',
            'zip': '682001',
            'phone': '9400662693',
            'street': 'Kochi Head Office',
        })
        cls.seller.write({'fulfilment_method': 'indiapost'})

    def setUp(self):
        super().setUp()
        self._ip_enable_stub_credentials()
        self.settings = self.env['logistics.indiapost.client']._ip_settings()

    def test_resolve_product_code_is_parcel_below_and_above_501_g(self):
        for grams in (1, 250, 500, 501, 1500):
            self.assertEqual(
                ipc.resolve_product_code(grams), ipc.PRODUCT_PARCEL, grams)
            self.assertNotEqual(
                ipc.resolve_product_code(grams), ipc.PRODUCT_DOC, grams)

    def test_resolve_article_type_redirects_speed_post_below_500_g(self):
        self.assertEqual(
            ipc.resolve_article_type(ipc.ARTICLE_TYPE_SPEED_POST, 499),
            ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(
            ipc.resolve_article_type(ipc.ARTICLE_TYPE_SPEED_POST, 0.499 * 1000),
            ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(
            ipc.resolve_article_type(ipc.ARTICLE_TYPE_SPEED_POST, 500),
            ipc.ARTICLE_TYPE_SPEED_POST)
        self.assertEqual(
            ipc.resolve_article_type(ipc.ARTICLE_TYPE_BUSINESS_PARCEL, 200),
            ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(
            ipc.resolve_article_type(ipc.ARTICLE_TYPE_BUSINESS_PARCEL, 1500),
            ipc.ARTICLE_TYPE_BUSINESS_PARCEL)

    def test_light_article_uses_parcel_shape_and_volumetric_weight(self):
        self.assertEqual(ipc.resolve_shape(250), ipc.SHAPE_RECTANGULAR)
        self.assertEqual(ipc.resolve_shape(500, cylindrical=True),
                         ipc.SHAPE_CYLINDRICAL)
        # 40 x 29 x 2 cm / 5 = 464 g; documents used to skip this.
        self.assertEqual(ipc.chargeable_weight_g(250, 40, 29, 2), 464)
        self.assertEqual(ipc.chargeable_weight_g(500, 14, 9, 1), 500)

    def test_light_article_must_meet_parcel_minimums_not_document_box(self):
        errors, warnings = ipc.validate_package(250, 10, 5, 5)
        self.assertTrue(errors)
        self.assertTrue(any('14' in error and '9' in error for error in errors))
        self.assertFalse(any('document' in warning.lower() for warning in warnings))

        errors, warnings = ipc.validate_package(250, 30, 21, 2)
        self.assertFalse(errors)
        self.assertFalse(any('document' in warning.lower() for warning in warnings))

    def test_booking_500_g_speed_post_stays_speed_post_parcel_shape(self):
        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'India Post Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Light article',
            'total_weight': 0.5,
            'length_cm': 14,
            'breadth_cm': 9,
            'height_cm': 1,
            'indiapost_article_type': ipc.ARTICLE_TYPE_SPEED_POST,
        })
        self.assertEqual(shipment.indiapost_product_code, ipc.PRODUCT_PARCEL)
        self.assertEqual(shipment.indiapost_shape, ipc.SHAPE_RECTANGULAR)
        self.assertEqual(shipment._ip_product(), ipc.ARTICLE_TYPE_SPEED_POST)

        article = shipment._ip_prepare_article(self.settings, 'ET214330016IN')
        self.assertEqual(article['article_type'], ipc.ARTICLE_TYPE_SPEED_POST)
        self.assertEqual(article['shape_of_article'], ipc.SHAPE_RECTANGULAR)
        self.assertNotEqual(article['shape_of_article'], ipc.SHAPE_DOC)
        self.assertEqual(article['physical_weight'], 500)

    def test_booking_below_500_g_speed_post_becomes_business_parcel(self):
        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'India Post Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Sub-500 Speed Post',
            'total_weight': 0.499,
            'length_cm': 14,
            'breadth_cm': 9,
            'height_cm': 1,
            'indiapost_article_type': ipc.ARTICLE_TYPE_SPEED_POST,
        })
        self.assertEqual(shipment._ip_product(), ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        article = shipment._ip_prepare_article(self.settings, 'CX214330016IN')
        self.assertEqual(article['article_type'], ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(article['shape_of_article'], ipc.SHAPE_RECTANGULAR)
        self.assertEqual(article['physical_weight'], 499)

    def test_booking_explicit_business_parcel_stays_below_500_g(self):
        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'India Post Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Chosen Business Parcel',
            'total_weight': 0.2,
            'length_cm': 14,
            'breadth_cm': 9,
            'height_cm': 1,
            'indiapost_article_type': ipc.ARTICLE_TYPE_BUSINESS_PARCEL,
        })
        self.assertEqual(shipment._ip_product(), ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        article = shipment._ip_prepare_article(self.settings, 'CX214330017IN')
        self.assertEqual(article['article_type'], ipc.ARTICLE_TYPE_BUSINESS_PARCEL)

    def test_quote_and_booking_article_agree_below_500_g(self):
        from unittest.mock import patch
        from odoo.addons.keralariders_logistics.models.indiapost_tariff import (
            BUSINESS_PARCEL_TARIFF_PATH,
        )
        from odoo.addons.keralariders_logistics.tests.test_indiapost_tariff import (
            BP_PAYLOAD, _response,
        )

        shipment = self.env['logistics.shipment'].create({
            'seller_id': self.seller.id,
            'shipping_to_name': 'India Post Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Agree quote book',
            'total_weight': 0.499,
            'length_cm': 14,
            'breadth_cm': 9,
            'height_cm': 1,
            'indiapost_article_type': ipc.ARTICLE_TYPE_SPEED_POST,
        })
        captured = []

        def fake_call(this, method, path, params=None, **kwargs):
            captured.append({'path': path, 'params': params})
            return _response(BP_PAYLOAD)

        with patch.object(self.registry['logistics.indiapost.client'],
                          'call', fake_call):
            quote = shipment._ip_quote_and_store(use_cache=False)
        article = shipment._ip_prepare_article(self.settings, 'CX214330018IN')

        self.assertEqual(captured[0]['path'], BUSINESS_PARCEL_TARIFF_PATH)
        self.assertEqual(quote['article_type'], ipc.ARTICLE_TYPE_BUSINESS_PARCEL)
        self.assertEqual(article['article_type'], quote['article_type'])
