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

No API calls: every quote here is stored the way ``_ip_quote_and_store`` would.
"""

from odoo import fields as odoo_fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.keralariders_logistics.models import indiapost_common as ipc


@tagged('post_install', '-at_install')
class TestIndiapostDeliveryCharge(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
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

        No India Post credentials are configured here, so the forced re-quote
        fails and that failure has to stop the debit.
        """
        self.assertTrue(self.shipment.indiapost_needs_quote)
        with self.assertRaises(UserError):
            self.shipment.action_add_wallet_transaction()
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
        with self.assertRaises(UserError):
            self.shipment.action_add_wallet_transaction()
        self.assertFalse(self.shipment.wallet_transaction_id)
