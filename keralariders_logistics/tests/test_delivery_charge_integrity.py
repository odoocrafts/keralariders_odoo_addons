"""A seller must not be able to decide what their own wallet is debited.

Regression tests for the tampering hole: portal sellers hold read, write and
create on ``logistics.shipment`` (they raise their own consignments), and the
delivery charge lived in ordinary writable columns that the wallet debit read
verbatim. Writing ``delivery_charges_subtotal``/``delivery_charges_total`` to
1.0 over RPC — or, through the legitimate compute, ``tax_percentage`` to -1 —
bought a pickup for a rupee or for nothing.

Both layers of the fix are exercised here, and so is the tension between them:
layer 1 refuses the write, layer 2 prices the shipment again at the moment of
the debit, and a price an administrator set by hand still has to survive layer 2.

``sudo()`` is used throughout the portal cases because that is what the portal
controllers do (``order.sudo().action_request_pickup()``), and sudo() leaves
``env.user`` as the real user, which is what the guard checks.
"""

from odoo.exceptions import AccessError, UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestDeliveryChargeIntegrity(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Shipment = cls.env['logistics.shipment']
        cls.Charges = cls.env['logistics.delivery.charges']

        cls.seller = cls.env['logistics.seller'].create({
            'name': 'Charge Guard Seller',
            'zip': '682001',
        })
        # This file is about the rate card, so the fixture is pinned to the hub
        # network wherever a carrier can be chosen at all: an India Post seller
        # is billed the postal tariff instead, which is covered separately in
        # test_indiapost_delivery_charge.
        if 'fulfilment_method' in cls.env['logistics.seller']._fields:
            cls.seller.write({'fulfilment_method': 'own_network'})
        cls.wallet = cls.seller.wallet_ids[0]
        cls.portal_user = cls.env['res.users'].create({
            'name': 'Charge Guard Portal',
            'login': 'kx_charge_guard_portal',
            'partner_id': cls.seller.partner_id.id,
            'group_ids': [(6, 0, [cls.env.ref('base.group_portal').id])],
        })
        # The fixtures only prove anything if the two users really do sit on
        # opposite sides of the group the guard checks.
        assert cls.env.user.has_group(
            'keralariders_logistics.group_logistics_admin'), \
            'the test user must be a logistics administrator'
        assert not cls.portal_user.has_group(
            'keralariders_logistics.group_logistics_admin'), \
            'the portal test user must not be a logistics administrator'

    def setUp(self):
        super().setUp()
        self._credit_wallet(5000.0)
        self.shipment = self._new_shipment()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _credit_wallet(self, amount):
        self.env['logistics.wallet.transaction'].create({
            'wallet_id': self.wallet.id,
            'amount': amount,
            'reference': 'Test top-up',
        })
        self.wallet.invalidate_recordset(['balance'])

    def _new_shipment(self, **overrides):
        vals = {
            'seller_id': self.seller.id,
            'shipping_to_name': 'Charge Guard Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.5,
        }
        vals.update(overrides)
        return self.Shipment.create(vals)

    def _rate_card(self, shipment=None):
        """What the rate card prices this shipment at, computed independently."""
        shipment = shipment or self.shipment
        return self.Charges.calculate_delivery_charge(
            shipment.total_weight,
            shipment.shipping_from_district_id == shipment.shipping_to_district_id,
            package_id=self.seller.delivery_package_id.id or None,
        )

    def _as_portal(self, shipment=None):
        return (shipment or self.shipment).with_user(self.portal_user)

    def _tamper(self, **columns):
        """Set the charge columns behind the ORM's back.

        Which is what a write path that layer 1 never sees would amount to —
        a migration, raw SQL, or a compute assigning the field while
        protected. Nothing is stamped, because no administrator set this price.
        """
        assignments = ', '.join('%s = %%s' % name for name in columns)
        # Flush first: pending computed values would otherwise be written out
        # on the next read and quietly undo the update.
        self.env.flush_all()
        self.env.cr.execute(
            'UPDATE logistics_shipment SET %s WHERE id = %%s' % assignments,
            list(columns.values()) + [self.shipment.id],
        )
        self.shipment.invalidate_recordset(list(columns))

    def _debit(self, shipment=None):
        shipment = shipment or self.shipment
        shipment.action_add_wallet_transaction()
        return shipment.wallet_transaction_id

    # ------------------------------------------------------------------
    # Layer 1: the write is refused
    # ------------------------------------------------------------------
    def test_portal_cannot_lower_the_subtotal(self):
        priced = self.shipment.delivery_charges_subtotal
        self.assertGreater(priced, 1.0, 'the fixture must cost more than a rupee')

        with self.assertRaises(AccessError):
            self._as_portal().write({'delivery_charges_subtotal': 1.0})
        with self.assertRaises(AccessError):
            self._as_portal().sudo().write({'delivery_charges_subtotal': 1.0})

        self.assertEqual(self.shipment.delivery_charges_subtotal, priced)

    def test_portal_cannot_lower_the_total(self):
        priced = self.shipment.delivery_charges_total

        with self.assertRaises(AccessError):
            self._as_portal().write({'delivery_charges_total': 1.0})
        with self.assertRaises(AccessError):
            self._as_portal().sudo().write({'delivery_charges_total': 1.0})

        self.assertEqual(self.shipment.delivery_charges_total, priced)

    def test_portal_cannot_zero_the_charge_through_tax_percentage(self):
        """The second route in: total = subtotal * (1 + tax_percentage).

        ``tax_percentage`` was an ordinary writable Float, so tax = -1 drove
        the total to zero through the legitimate compute without ever touching
        a readonly field. Guarding only the two charge fields left this open.
        """
        priced = self.shipment.delivery_charges_total

        with self.assertRaises(AccessError):
            self._as_portal().write({'tax_percentage': -1.0})
        with self.assertRaises(AccessError):
            self._as_portal().sudo().write({'tax_percentage': -1.0})

        self.assertEqual(self.shipment.tax_percentage, 0.0)
        self.assertEqual(self.shipment.delivery_charges_total, priced)

    def test_portal_cannot_reuse_a_wallet_transaction_to_skip_the_debit(self):
        """The link is what records that a shipment was paid for.

        ``action_add_wallet_transaction`` is a no-op when it is already set, so
        pointing it at any existing transaction bought a free pickup without
        touching an amount at all.
        """
        other = self._new_shipment()
        self._debit(other)

        with self.assertRaises(AccessError):
            self._as_portal().sudo().write({
                'wallet_transaction_id': other.wallet_transaction_id.id,
            })
        self.assertFalse(self.shipment.wallet_transaction_id)

    def test_portal_cannot_create_a_shipment_ready_priced(self):
        """Portal creates run sudoed, so create needs the same guard."""
        with self.assertRaises(AccessError):
            self.Shipment.with_user(self.portal_user).sudo().create({
                'seller_id': self.seller.id,
                'shipping_to_name': 'Charge Guard Customer',
                'shipping_to_address': '12 Test Road, Test Nagar',
                'shipping_to_zip': '695001',
                'shipping_to_mobile': '9876543210',
                'item_description': 'Test article',
                'total_weight': 1.5,
                'delivery_charges_subtotal': 1.0,
                'delivery_charges_total': 1.0,
            })

    def test_sellers_keep_declaring_their_own_parcels(self):
        """The declarations the charge is derived *from* stay seller-writable.

        Weight, order value and COD are the seller's to state — the fix must
        not turn raising a consignment into an admin job.
        """
        self._as_portal().sudo().write({
            'total_weight': 3.0,
            'total_order_value': 1200.0,
            'order_payment_type': 'cod',
            'cod_amount': 1200.0,
            'shipping_to_address': '13 Test Road, Test Nagar',
        })
        self.assertEqual(self.shipment.total_weight, 3.0)
        self.assertEqual(self.shipment.cod_amount, 1200.0)
        # ...and the charge follows the declaration, upwards included.
        self.assertEqual(self.shipment.delivery_charges_total,
                         self._rate_card())

    def test_a_portal_seller_can_still_request_pickup(self):
        """The production path end to end, as /my/orders/<id> drives it.

        ``order.sudo().action_request_pickup()`` debits the wallet and moves
        the shipment while env.user is the seller, so a guard drawn even
        slightly too wide would stop every seller shipping anything.
        """
        as_seller = self.env['logistics.order'].with_user(self.portal_user).sudo()
        order = as_seller.create({'seller_id': self.seller.id})
        shipment = self.Shipment.with_user(self.portal_user).sudo().create({
            'order_id': order.id,
            'seller_id': self.seller.id,
            'shipping_to_name': 'Charge Guard Customer',
            'shipping_to_address': '12 Test Road, Test Nagar',
            'shipping_to_zip': '695001',
            'shipping_to_mobile': '9876543210',
            'item_description': 'Test article',
            'total_weight': 1.5,
            'state': 'order_added',
        })
        charge = shipment.delivery_charges_total
        self.assertGreater(charge, 1.0)
        opening_balance = self.wallet.balance

        order.with_user(self.portal_user).sudo().action_request_pickup()

        self.assertTrue(shipment.wallet_transaction_id,
                        'the seller could not pay for their own pickup')
        self.assertAlmostEqual(shipment.wallet_transaction_id.amount, -charge,
                               places=2)
        self.assertEqual(shipment.state, 'pickup_requested')
        self.wallet.invalidate_recordset(['balance'])
        self.assertAlmostEqual(self.wallet.balance, opening_balance - charge,
                               places=2)

    # ------------------------------------------------------------------
    # Layer 1: ops overrides survive
    # ------------------------------------------------------------------
    def test_administrator_can_still_price_by_hand(self):
        manual = self._rate_card() + 25.0
        self.shipment.write({
            'delivery_charges_subtotal': manual,
            'delivery_charges_total': manual,
        })
        self.assertEqual(self.shipment.delivery_charges_total, manual)
        self.assertEqual(self.shipment.delivery_charge_override_amount, manual)
        self.assertEqual(self.shipment.delivery_charge_override_uid, self.env.user)
        self.assertTrue(self.shipment.delivery_charge_override_signature)

    def test_a_form_save_that_changes_nothing_is_not_an_override(self):
        """``force_save="1"`` re-sends the charge fields on every backend save.

        If that counted as an override, every saved shipment would be exempt
        from layer 2 and the fix would be worth nothing.
        """
        self.shipment.write({
            'delivery_charges_subtotal': self.shipment.delivery_charges_subtotal,
            'delivery_charges_total': self.shipment.delivery_charges_total,
            'tax_percentage': self.shipment.tax_percentage,
        })
        self.assertFalse(self.shipment.delivery_charge_override_signature)

    def test_an_ops_weight_correction_is_not_an_override(self):
        """Ops fix a weight on the form; the client sends the new charge with it."""
        reweighed = self._new_shipment(total_weight=3.0)
        self.shipment.write({
            'total_weight': 3.0,
            'delivery_charges_subtotal': reweighed.delivery_charges_subtotal,
            'delivery_charges_total': reweighed.delivery_charges_total,
        })
        self.assertFalse(self.shipment.delivery_charge_override_signature)
        self.assertEqual(self.shipment.delivery_charges_total, self._rate_card())

    # ------------------------------------------------------------------
    # Layer 2: the debit is priced afresh
    # ------------------------------------------------------------------
    def test_a_tampered_column_cannot_reach_the_wallet(self):
        """Layer 2 alone, with layer 1 bypassed entirely.

        The columns are set behind the ORM's back, which is what a write path
        missed by layer 1 would amount to. No override is stamped, because no
        administrator set this price.
        """
        expected = self._rate_card()
        self._tamper(delivery_charges_subtotal=1.0, delivery_charges_total=1.0)
        self.assertEqual(self.shipment.delivery_charges_total, 1.0)

        transaction = self._debit()
        self.assertEqual(transaction.amount, -expected,
                         'the wallet was debited from the tampered column')
        # ...and the shipment is healed, so the seller is not left with a
        # record that disagrees with what they paid.
        self.assertEqual(self.shipment.delivery_charges_total, expected)
        self.assertEqual(self.shipment.delivery_charges_subtotal, expected)

    def test_a_tampered_column_is_reported_in_the_chatter(self):
        before = self.shipment.message_ids
        self._tamper(delivery_charges_total=1.0)

        self._debit()
        notes = self.shipment.message_ids - before
        self.assertTrue(
            any('did not match the rate card' in (m.body or '') for m in notes),
            'the discrepancy was not recorded anywhere an administrator looks',
        )

    def test_the_balance_check_uses_the_recomputed_charge(self):
        """A tampered charge must not buy a pickup the wallet cannot cover."""
        self._credit_wallet(-self.wallet.balance + 10.0)
        self._tamper(delivery_charges_total=1.0)

        with self.assertRaises(UserError):
            self._debit()
        self.assertFalse(self.shipment.wallet_transaction_id)

    def test_a_weightless_shipment_cannot_be_debited(self):
        """Nothing to price it from, so the pickup is refused rather than free.

        Weight stays seller-declared, which means declaring none used to buy a
        free shipment: the compute leaves the charge at 0.00 and the debit
        followed. The portal form already rejects a zero weight; the bulk
        upload did not.
        """
        weightless = self._new_shipment(total_weight=0.0)
        with self.assertRaises(UserError):
            self._debit(weightless)
        self.assertFalse(weightless.wallet_transaction_id)

    # ------------------------------------------------------------------
    # The crux: an override has to survive an unconditional recompute
    # ------------------------------------------------------------------
    def test_an_ops_override_is_still_honoured_at_debit(self):
        """Layer 2 recomputes, but not over a price ops deliberately set."""
        manual = self._rate_card() - 20.0
        self.shipment.write({
            'delivery_charges_subtotal': manual,
            'delivery_charges_total': manual,
        })

        transaction = self._debit()
        self.assertEqual(transaction.amount, -manual,
                         'layer 2 recomputed away a legitimate ops override')

    def test_an_override_lapses_when_the_shipment_it_priced_changes(self):
        """The override is dated by its inputs, like the India Post quote.

        Without this, an ops discount on a 1.5 kg parcel would keep applying
        after the seller reweighed it to 10 kg.
        """
        manual = self._rate_card() - 20.0
        self.shipment.write({
            'delivery_charges_subtotal': manual,
            'delivery_charges_total': manual,
        })
        self.assertTrue(self.shipment._delivery_charge_override_applies())

        self._as_portal().sudo().write({'total_weight': 5.0})
        self.assertFalse(self.shipment._delivery_charge_override_applies())

        transaction = self._debit()
        self.assertEqual(transaction.amount, -self._rate_card())
        self.assertFalse(self.shipment.delivery_charge_override_signature,
                         'a lapsed override must not be left behind')

    def test_an_override_cannot_be_forged_by_a_seller(self):
        """The stamp is what separates an override from tampering.

        Which makes the stamp itself worth exactly as much as the charge: a
        seller who could write the provenance fields would price their own
        parcel one indirection further out, and layer 2 would honour it.
        """
        with self.assertRaises(AccessError):
            self._as_portal().sudo().write({
                'delivery_charge_override_amount': 1.0,
                'delivery_charge_override_signature':
                    self.shipment._delivery_charge_signature(),
            })
        self.assertFalse(self.shipment.delivery_charge_override_signature)

        transaction = self._debit()
        self.assertEqual(transaction.amount, -self._rate_card())
