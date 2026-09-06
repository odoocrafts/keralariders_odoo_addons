"""India Post booking and labelling on ``logistics.shipment``.

The hub network is untouched: a shipment whose seller is on ``own_network``
behaves exactly as before. Only ``fulfilment_method == 'indiapost'`` shipments
go anywhere near this code.

Address model for a booking, as decided by the business:
  * consignor of record is KeralaXpress, from the settings screen;
  * pickup happens at the seller's own premises (``pickup_address_flag``);
  * the alternate address is the seller, so undelivered articles come back to
    the seller and not to us (``alt_address_flag``).
Those are three independent address groups in one article, which the sandbox
validator accepts.
"""

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError

import base64
import logging

from . import indiapost_common as ipc
from .indiapost_client import IndiapostApiError
from .seller import FULFILMENT_ADMIN_GROUP

_logger = logging.getLogger(__name__)

BOOKING_PATH_TEMPLATE = '/process-articles/%s'  # note: no /v1 prefix
LABEL_PATH = '/v1/label/create/domestic'

# The file-upload variant of the booking endpoint takes 5000 articles; the JSON
# body variant has no documented ceiling, so keep request bodies modest.
BOOKING_CHUNK_SIZE = 200

TERMINAL_STATES = ('delivered', 'cancelled', 'returned')

# Bumped whenever the meaning of a signature component changes, so quotes
# stored by an older version of this code are treated as stale rather than
# silently trusted.
QUOTE_SIGNATURE_VERSION = 'v1'

# An India Post shipment is billed the postal tariff rather than the slab
# table, which makes the stored quote the price: these fields decide what the
# seller pays and are added to the delivery charge guard on logistics.shipment.
# The two staleness fields belong here as much as the amounts do — a seller who
# could backdate the signature would make a forged tariff look freshly quoted
# and walk straight past the re-quote that protects the debit.
INDIAPOST_CHARGE_FIELDS = (
    'indiapost_base_tariff',
    'indiapost_vas_charges',
    'indiapost_tax_amount',
    'indiapost_total_tariff',
    'indiapost_quote_signature',
    'indiapost_tariff_quoted_on',
)


def quote_failure_reason(exc):
    """The most useful sentence available from a failed rate lookup."""
    if isinstance(exc, IndiapostApiError):
        return exc.user_message()
    if exc.args:
        return str(exc.args[0])
    return str(exc)


class Shipment(models.Model):
    _inherit = 'logistics.shipment'

    # ------------------------------------------------------------------
    # Fulfilment
    # ------------------------------------------------------------------
    fulfilment_method = fields.Selection(
        [('indiapost', 'India Post (Speed Post)'),
         ('own_network', 'KeralaXpress Hub Network')],
        string='Fulfilment Method',
        default='indiapost',
        required=True,
        index=True,
        copy=False,
        tracking=True,
        help='Resolved from the seller when the shipment is created and then '
             'left alone, so historical shipments stay accurate if the seller '
             'is moved to a different carrier later. Administrators can divert '
             'a single shipment to the hub network if India Post refuses it.',
    )
    is_indiapost = fields.Boolean(
        string='Ships via India Post', compute='_compute_is_indiapost',
    )

    @api.depends('fulfilment_method')
    def _compute_is_indiapost(self):
        for record in self:
            record.is_indiapost = record.fulfilment_method == 'indiapost'

    def _ip_can_set_fulfilment_method(self):
        """Whether the current user may choose a shipment's carrier.

        Delegates to ``logistics.seller`` so the shipment and the seller share
        one definition of "trusted", including the
        ``allow_fulfilment_method_write`` context key. Like the seller guard it
        checks the *real* user, so a ``sudo()`` made while serving a portal
        request is still refused.

        Deliberately no field-level ``groups`` here, unlike the seller field:
        the shipment's carrier is shown in the backend list and search views
        and grouped by, and hub managers and delivery executives legitimately
        need to see which carrier is carrying a package. Read stays open; the
        write guards below are the control.
        """
        return self.env['logistics.seller']._ip_can_set_fulfilment_method()

    def _ip_check_fulfilment_method_write(self, vals):
        if 'fulfilment_method' in vals and not self._ip_can_set_fulfilment_method():
            raise AccessError(_(
                "Only a Logistics Administrator can change a shipment's "
                "fulfilment method. Please contact KeralaXpress support."
            ))

    # ------------------------------------------------------------------
    # Dimensions
    # ------------------------------------------------------------------
    length_cm = fields.Float(string='Length (cm)', digits=(16, 1))
    breadth_cm = fields.Float(string='Breadth / Diameter (cm)', digits=(16, 1))
    height_cm = fields.Float(string='Height (cm)', digits=(16, 1))
    is_cylindrical = fields.Boolean(
        string='Cylindrical Package',
        help='Tubes and rolls are booked as ROLL rather than NROL.',
    )
    total_dimension_cm = fields.Integer(
        string='L + B + H (cm)', compute='_compute_indiapost_package', store=True,
    )
    volumetric_weight_g = fields.Integer(
        string='Volumetric Weight (g)', compute='_compute_indiapost_package',
        store=True, help='Length x breadth x height in cm, divided by 5.',
    )
    chargeable_weight_g = fields.Integer(
        string='Chargeable Weight (g)', compute='_compute_indiapost_package',
        store=True,
        help='What India Post bills: the actual weight for documents up to '
             '500 g, and the greater of actual and volumetric weight for '
             'parcels above that.',
    )
    indiapost_product_code = fields.Char(
        string='Speed Post Product', compute='_compute_indiapost_package',
        store=True,
        help='Chosen by India Post from the physical weight: documents up to '
             '500 g, parcels above that.',
    )
    indiapost_shape = fields.Char(
        string='Shape Code', compute='_compute_indiapost_package', store=True,
    )
    indiapost_package_warning = fields.Text(
        string='Packaging Warning', compute='_compute_indiapost_package_check',
    )
    indiapost_package_error = fields.Text(
        string='Packaging Problem', compute='_compute_indiapost_package_check',
    )

    @api.depends('total_weight', 'length_cm', 'breadth_cm', 'height_cm',
                 'is_cylindrical')
    def _compute_indiapost_package(self):
        for record in self:
            grams = ipc.kg_to_grams(record.total_weight)
            length = ipc.cm_to_int(record.length_cm)
            breadth = ipc.cm_to_int(record.breadth_cm)
            height = ipc.cm_to_int(record.height_cm)
            record.total_dimension_cm = length + breadth + height
            record.volumetric_weight_g = ipc.volumetric_weight_g(
                length, breadth, height)
            # Documents are billed on actual weight however bulky they are.
            record.chargeable_weight_g = ipc.chargeable_weight_g(
                grams, length, breadth, height)
            record.indiapost_product_code = ipc.resolve_product_code(grams)
            record.indiapost_shape = ipc.resolve_shape(
                grams, cylindrical=record.is_cylindrical)

    @api.depends('total_weight', 'length_cm', 'breadth_cm', 'height_cm',
                 'fulfilment_method')
    def _compute_indiapost_package_check(self):
        for record in self:
            if record.fulfilment_method != 'indiapost' or not record.total_weight:
                record.indiapost_package_error = False
                record.indiapost_package_warning = False
                continue
            errors, warnings = ipc.validate_package(
                ipc.kg_to_grams(record.total_weight),
                record.length_cm, record.breadth_cm, record.height_cm,
            )
            record.indiapost_package_error = '\n'.join(errors) or False
            record.indiapost_package_warning = '\n'.join(warnings) or False

    @api.constrains('length_cm', 'breadth_cm', 'height_cm', 'total_weight',
                    'fulfilment_method')
    def _check_indiapost_package(self):
        """Refuse packages India Post physically cannot carry.

        This is the only place the rule is enforced end to end: the booking
        endpoint accepts a 600 g article measuring 10 x 5 x 5 cm without a
        murmur, and it is only the tariff endpoint (and the counter clerk) that
        reject it.
        """
        for record in self:
            if record.fulfilment_method != 'indiapost':
                continue
            if not (record.length_cm or record.breadth_cm or record.height_cm):
                # Legacy shipments predate these fields; only validate once
                # somebody starts filling them in.
                continue
            errors, _warnings = ipc.validate_package(
                ipc.kg_to_grams(record.total_weight),
                record.length_cm, record.breadth_cm, record.height_cm,
            )
            if errors:
                raise ValidationError(
                    _('Shipment %s cannot be carried by India Post:\n\n%s')
                    % (record.name, '\n'.join('• %s' % e for e in errors))
                )

    # ------------------------------------------------------------------
    # Pickup scheduling
    # ------------------------------------------------------------------
    indiapost_pickup_slot = fields.Selection(
        ipc.PICKUP_SLOTS, string='Pickup Slot', default='10:00-13:00',
        help='India Post accepts these two slots only.',
    )
    indiapost_pickup_date = fields.Date(
        string='Requested Pickup Date',
        help='India Post does not sanity-check this date, so it is validated '
             'here instead: it must not be in the past.',
    )

    @api.constrains('indiapost_pickup_date', 'fulfilment_method')
    def _check_indiapost_pickup_date(self):
        today = fields.Date.context_today(self)
        for record in self:
            if record.fulfilment_method != 'indiapost':
                continue
            if record.indiapost_pickup_date and record.indiapost_pickup_date < today:
                raise ValidationError(_(
                    'The India Post pickup date for %(awb)s is %(date)s, which '
                    'is in the past. India Post accepts past dates silently and '
                    'the pickup would simply never happen.'
                ) % {'awb': record.name, 'date': record.indiapost_pickup_date})

    def _ip_default_pickup_date(self):
        """Next working pickup date, honouring the configured lead time."""
        self.ensure_one()
        settings = self.env['logistics.indiapost.client']._ip_settings()
        lead = settings['indiapost_pickup_lead_days']
        base = self.order_id.pickup_date or fields.Date.context_today(self)
        earliest = fields.Date.add(fields.Date.context_today(self), days=lead)
        return max(base, earliest)

    # ------------------------------------------------------------------
    # Booking results
    # ------------------------------------------------------------------
    indiapost_article_number = fields.Char(
        string='India Post Article Number', copy=False, index=True, readonly=True,
        help='The 13-character Speed Post barcode carried on the label.',
    )
    indiapost_barcode_id = fields.Many2one(
        'logistics.indiapost.barcode', string='Allocated Barcode', copy=False,
        readonly=True, ondelete='set null',
    )
    indiapost_booking_state = fields.Selection(
        [
            ('not_required', 'Not Applicable'),
            ('to_book', 'Ready to Book'),
            ('booked', 'Booked'),
            ('error', 'Booking Failed'),
        ],
        string='India Post Booking', default='to_book', copy=False, index=True,
        readonly=True, tracking=True,
    )
    indiapost_batch_id = fields.Char(string='Batch Id', copy=False, readonly=True)
    indiapost_correlation_id = fields.Char(
        string='Correlation Id', copy=False, readonly=True,
        help='Quote this to India Post support when chasing a failed booking.',
    )
    indiapost_mail_booking_dom_id = fields.Char(
        string='Mail Booking Id', copy=False, readonly=True,
    )
    indiapost_offset_number = fields.Char(string='Offset No.', copy=False,
                                          readonly=True)
    indiapost_block_number = fields.Char(string='Block No.', copy=False,
                                         readonly=True)
    indiapost_calculated_tariff = fields.Monetary(
        string='Tariff Charged by India Post', currency_field='currency_id',
        copy=False, readonly=True,
    )
    indiapost_booked_on = fields.Datetime(string='Booked On', copy=False,
                                          readonly=True)
    indiapost_booking_error = fields.Text(string='Booking Errors', copy=False,
                                          readonly=True)

    # ------------------------------------------------------------------
    # Tariff snapshot
    # ------------------------------------------------------------------
    indiapost_base_tariff = fields.Monetary(
        string='Base Tariff', currency_field='currency_id', copy=False)
    indiapost_vas_charges = fields.Monetary(
        string='Value Added Services', currency_field='currency_id', copy=False)
    indiapost_tax_amount = fields.Monetary(
        string='GST', currency_field='currency_id', copy=False)
    indiapost_total_tariff = fields.Monetary(
        string='India Post Total', currency_field='currency_id', copy=False)
    indiapost_quoted_weight_g = fields.Integer(string='Quoted Weight (g)',
                                               copy=False)
    indiapost_quoted_chargeable_g = fields.Integer(
        string='Quoted Chargeable Weight (g)', copy=False)
    indiapost_distance_display = fields.Char(
        string='Distance', copy=False,
        help='As returned by India Post. Normally kilometres, but the literal '
             'string "OS" also occurs.',
    )
    indiapost_tariff_quoted_on = fields.Datetime(string='Rate Quoted On',
                                                 copy=False)
    indiapost_quote_signature = fields.Char(
        string='Quoted Inputs', copy=False, readonly=True,
        help='Fingerprint of every input India Post priced this shipment on. '
             'Kept as readable text rather than a hash so a stale quote can be '
             'diagnosed by eye. The stored value is compared with the current '
             'one to decide whether the price still applies.',
    )
    indiapost_needs_quote = fields.Boolean(
        string='Needs a Rate Quote', compute='_compute_indiapost_needs_quote',
    )

    def _ip_quote_signature_parts(self):
        """Every input that can move the India Post price, in a fixed order.

        Deliberately exhaustive rather than clever: a new pricing input is
        added here and staleness detection follows automatically, instead of
        needing a matching comparison to be remembered somewhere else.
        """
        self.ensure_one()
        return (
            QUOTE_SIGNATURE_VERSION,
            # Weight is banded because that is the weight actually quoted; a
            # 1 g edit inside the same 50 g postal step cannot change the price.
            'w%d' % ipc.band_weight(ipc.kg_to_grams(self.total_weight)),
            'l%d' % ipc.cm_to_int(self.length_cm),
            'b%d' % ipc.cm_to_int(self.breadth_cm),
            'h%d' % ipc.cm_to_int(self.height_cm),
            # Paise, so a rounding difference cannot hide a real change.
            'ins%d' % round((self.indiapost_insurance_value or 0.0) * 100),
            'pod%d' % bool(self.indiapost_vas_pod),
            'reg%d' % bool(self.indiapost_vas_reg),
            'ack%d' % bool(self.indiapost_vas_ack),
            'otp%d' % bool(self.indiapost_vas_otp),
            'from%s' % (self._ip_origin_pincode_soft() or '-'),
            'to%s' % ((self.shipping_to_zip or '').strip() or '-'),
        )

    def _ip_quote_signature(self):
        self.ensure_one()
        return '|'.join(self._ip_quote_signature_parts())

    def _ip_origin_pincode_soft(self):
        """:meth:`_ip_origin_pincode` for contexts that must not raise.

        The signature is computed on half-filled records too (a draft with no
        seller pincode yet), and an unresolvable origin is simply part of the
        fingerprint: filling it in later invalidates the quote, correctly.
        """
        self.ensure_one()
        try:
            return self._ip_origin_pincode()
        except ipc.IndiapostDataError:
            return ''

    @api.depends('fulfilment_method', 'indiapost_tariff_quoted_on',
                 'indiapost_quote_signature', 'indiapost_article_number',
                 'indiapost_booking_state', 'total_weight', 'length_cm',
                 'breadth_cm', 'height_cm', 'indiapost_insurance_value',
                 'indiapost_vas_pod', 'indiapost_vas_reg',
                 'indiapost_vas_ack', 'indiapost_vas_otp',
                 'shipping_from_zip', 'shipping_to_zip', 'seller_id.zip')
    def _compute_indiapost_needs_quote(self):
        for record in self:
            if record.fulfilment_method != 'indiapost':
                record.indiapost_needs_quote = False
                continue
            if record.indiapost_article_number \
                    or record.indiapost_booking_state == 'booked':
                # India Post has already priced this article at booking and
                # indiapost_calculated_tariff holds their figure, so there is
                # nothing left to re-quote — a fresh tariff lookup would only
                # disagree with what was actually charged.
                record.indiapost_needs_quote = False
                continue
            if not record.indiapost_tariff_quoted_on:
                record.indiapost_needs_quote = True
                continue
            record.indiapost_needs_quote = (
                record.indiapost_quote_signature != record._ip_quote_signature()
            )

    # ------------------------------------------------------------------
    # Value added services
    # ------------------------------------------------------------------
    indiapost_insurance_value = fields.Monetary(
        string='Declared Value for Insurance', currency_field='currency_id',
        help='India Post insurance is roughly 6% of the declared value and can '
             'easily exceed the postage. Leave at zero for no insurance.',
    )
    indiapost_vas_pod = fields.Boolean(string='Proof of Delivery')
    indiapost_vas_reg = fields.Boolean(string='Registered')
    indiapost_vas_ack = fields.Boolean(string='Acknowledgement')
    indiapost_vas_otp = fields.Boolean(string='OTP Delivery')

    def _ip_vas_flags(self):
        self.ensure_one()
        return {
            'pod': self.indiapost_vas_pod,
            'reg': self.indiapost_vas_reg,
            'ack': self.indiapost_vas_ack,
            'otp': self.indiapost_vas_otp,
        }

    # ------------------------------------------------------------------
    # Offices
    # ------------------------------------------------------------------
    indiapost_pickup_office_id = fields.Char(string='Pickup Office Id',
                                             readonly=True, copy=False)
    indiapost_pickup_office_name = fields.Char(string='Pickup Office',
                                               readonly=True, copy=False)
    indiapost_dest_office_id = fields.Char(string='Destination Office Id',
                                           readonly=True, copy=False)
    indiapost_dest_office_name = fields.Char(string='Destination Office',
                                             readonly=True, copy=False)

    # ------------------------------------------------------------------
    # Label
    # ------------------------------------------------------------------
    indiapost_label_pdf = fields.Binary(string='India Post Label', attachment=True,
                                        copy=False, readonly=True)
    indiapost_label_filename = fields.Char(string='Label Filename', copy=False,
                                           readonly=True)
    indiapost_label_size = fields.Selection(
        [('A6', 'A6'), ('A7', 'A7')], string='Label Size', copy=False,
    )
    indiapost_label_fetched_on = fields.Datetime(string='Label Fetched On',
                                                 copy=False, readonly=True)

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------
    indiapost_last_tracking_sync = fields.Datetime(
        string='Last Tracking Sync', copy=False, readonly=True, index=True,
    )
    indiapost_del_status = fields.Char(
        string='India Post Delivery Status', copy=False, readonly=True,
        help='Raw del_status. "not delivered" is returned for unbooked '
             'articles too, so it means very little on its own.',
    )
    indiapost_tracking_ref = fields.Char(
        string='India Post Tracking Link', compute='_compute_indiapost_tracking_ref',
    )

    @api.depends('indiapost_article_number')
    def _compute_indiapost_tracking_ref(self):
        for record in self:
            record.indiapost_tracking_ref = (
                'https://www.indiapost.gov.in/_layouts/15/DOP.Portal.Tracking/'
                'TrackConsignment.aspx?logisticsRefNo=%s'
                % record.indiapost_article_number
            ) if record.indiapost_article_number else False

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        """Resolve the carrier from the seller unless the caller is trusted.

        A portal seller may only ever get the carrier their seller record says,
        so a caller-supplied ``fulfilment_method`` is discarded rather than
        refused: ``/my/shipments/create`` and the bulk upload are legitimate
        portal creates and must keep working. Only an administrator (or server
        code opting in through ``allow_fulfilment_method_write``) can pin the
        carrier explicitly, which is what a manual backend create needs.
        """
        Seller = self.env['logistics.seller'].sudo()
        trusted = self._ip_can_set_fulfilment_method()
        for vals in vals_list:
            if not trusted or not vals.get('fulfilment_method'):
                seller = Seller.browse(vals['seller_id']) if vals.get('seller_id') \
                    else Seller.browse()
                vals['fulfilment_method'] = (
                    seller.fulfilment_method if seller.exists() else 'indiapost'
                ) or 'indiapost'
            if vals['fulfilment_method'] != 'indiapost':
                vals.setdefault('indiapost_booking_state', 'not_required')
        return super().create(vals_list)

    def write(self, vals):
        # Unlike create, a write cannot be silently corrected: the caller asked
        # for a carrier change on an existing shipment and has to be told no.
        self._ip_check_fulfilment_method_write(vals)
        return super().write(vals)

    # ------------------------------------------------------------------
    # Delivery charges
    # ------------------------------------------------------------------
    @api.depends('total_weight', 'shipping_from_district_id',
                 'shipping_to_district_id', 'tax_percentage',
                 'fulfilment_method', 'indiapost_base_tariff',
                 'indiapost_vas_charges', 'indiapost_total_tariff')
    def _compute_delivery_charges(self):
        """India Post shipments bill the real postal tariff, not the slab table.

        The slab logic is untouched for hub-network sellers, so switching a
        seller back to the hub network needs no data migration.
        """
        indiapost = self.filtered(lambda s: s.fulfilment_method == 'indiapost')
        for record in indiapost:
            record.delivery_charges_subtotal = (
                record.indiapost_base_tariff + record.indiapost_vas_charges
            )
            record.delivery_charges_total = (
                record.indiapost_total_tariff
                or record.delivery_charges_subtotal
            )
        super(Shipment, self - indiapost)._compute_delivery_charges()

    def _delivery_charge_guarded_fields(self):
        """Add the postal tariff to the staff-only charge fields.

        For an India Post shipment the stored quote *is* the price, so leaving
        it writable would have reopened the tampering hole one field along:
        ``indiapost_total_tariff = 1`` prices the parcel at a rupee through the
        legitimate compute, exactly as ``tax_percentage = -1`` did.
        """
        return super()._delivery_charge_guarded_fields() + INDIAPOST_CHARGE_FIELDS

    def _delivery_charge_signature_parts(self):
        """Date a manual price by the postal inputs as well as the slab ones.

        An override on an India Post shipment has to lapse on everything that
        moves the postal tariff — dimensions, insurance, VAS, either pincode —
        not just on weight and district. Reusing the quote signature means the
        override and the tariff go stale on precisely the same events, so the
        two staleness checks can never disagree about whether the shipment
        still is the one that was priced.
        """
        parts = super()._delivery_charge_signature_parts()
        if self.fulfilment_method == 'indiapost':
            parts.append('ip%s' % self._ip_quote_signature())
        return parts

    def _authoritative_delivery_charge(self):
        """Price an India Post shipment from the postal tariff, not the slab.

        Layer 2 of the delivery charge guard recomputes the charge immediately
        before the wallet is debited. Left to the base implementation it would
        recompute the *slab* price and bill an India Post parcel at hub-network
        rates, so the recompute follows the same split as the compute: the
        postal tariff here, the rate card for everyone else.

        The tariff is trustworthy at this point for two separate reasons: the
        fields are staff-only (see :meth:`_delivery_charge_guarded_fields`), and
        :meth:`action_add_wallet_transaction` refuses to reach this code with a
        quote whose signature does not match the shipment.
        """
        self.ensure_one()
        if self.fulfilment_method != 'indiapost':
            return super()._authoritative_delivery_charge()
        subtotal = self.indiapost_base_tariff + self.indiapost_vas_charges
        total = self.indiapost_total_tariff or subtotal
        return self._round_charge(subtotal), self._round_charge(total)

    def action_add_wallet_transaction(self):
        """Never debit a wallet against a stale or missing India Post rate.

        The stored quote signature is the authority: if it does not match the
        shipment as it stands now, the price is re-fetched before a single
        rupee moves, and a failure to re-fetch stops the debit outright rather
        than charging yesterday's number.
        """
        for record in self:
            if record.fulfilment_method != 'indiapost':
                continue
            if not record.indiapost_needs_quote:
                continue
            if record._delivery_charge_override_applies():
                # An administrator has priced this parcel by hand, so the
                # postal tariff is not what the seller is being billed and a
                # stale one cannot make the debit wrong. Refreshing it anyway
                # would strand the shipment whenever the India Post API is
                # unreachable — the exact situation a manual price is for. The
                # override carries the quote signature (see
                # _delivery_charge_signature_parts), so it has already lapsed
                # if the article itself changed since it was set.
                continue
            try:
                record._ip_quote_and_store()
            except (UserError, ValidationError, ipc.IndiapostDataError,
                    IndiapostApiError) as exc:
                raise UserError(_(
                    'The India Post rate for %(awb)s is out of date because '
                    'the package details changed after it was quoted, and a '
                    'fresh rate could not be fetched:\n\n%(reason)s\n\n'
                    'Nothing has been charged. Correct the package details or '
                    'use "Refresh India Post Rate", then try again.'
                ) % {'awb': record.name,
                     'reason': quote_failure_reason(exc)}) from exc
            if record.indiapost_needs_quote \
                    and not record._delivery_charge_override_applies():
                # Belt and braces: a stored quote whose signature still does
                # not match means the debit would be against the wrong price.
                raise UserError(_(
                    'The India Post rate stored for %s does not match the '
                    'package as it stands, so the wallet has not been '
                    'debited. Refresh the India Post rate and try again.'
                ) % record.name)
        return super().action_add_wallet_transaction()

    # ------------------------------------------------------------------
    # Rate quoting
    # ------------------------------------------------------------------
    def _ip_quote_and_store(self, use_cache=True):
        """Fetch the live tariff for this shipment and store the breakdown."""
        self.ensure_one()
        settings = self.env['logistics.indiapost.client']._ip_require_configured()
        origin = self._ip_origin_pincode()
        # Taken before the request so the fingerprint describes exactly the
        # article that was priced, whatever happens afterwards.
        signature = self._ip_quote_signature()
        quote = self.env['logistics.indiapost.tariff'].quote(
            origin, self.shipping_to_zip,
            weight_kg=self.total_weight,
            length_cm=self.length_cm,
            breadth_cm=self.breadth_cm,
            height_cm=self.height_cm,
            insurance_value=self.indiapost_insurance_value,
            use_cache=use_cache,
            shipment=self,
            settings=settings,
            **self._ip_vas_flags()
        )
        # The tariff fields are staff-only, and this runs on behalf of a portal
        # seller requesting pickup: the authorisation for this write is that
        # the figures come straight from India Post, so it opts in explicitly.
        self.with_context(allow_delivery_charge_write=True).write({
            'indiapost_base_tariff': quote['base_tariff'],
            'indiapost_vas_charges': quote['vas_charges'],
            'indiapost_tax_amount': quote['total_tax'],
            'indiapost_total_tariff': quote['total_payable'],
            'indiapost_quoted_weight_g': quote['billed_weight_g'],
            'indiapost_quoted_chargeable_g': quote['chargeable_weight_g'],
            'indiapost_distance_display': quote['distance_display'],
            'indiapost_tariff_quoted_on': fields.Datetime.now(),
            'indiapost_quote_signature': signature,
        })
        return quote

    def action_indiapost_quote(self):
        """Form button: refresh the India Post rate, bypassing the cache."""
        for record in self:
            if record.fulfilment_method != 'indiapost':
                raise UserError(_(
                    'Shipment %s is fulfilled through the KeralaXpress hub '
                    'network, so there is no India Post rate to fetch.'
                ) % record.name)
            record._ip_quote_and_store(use_cache=False)
        return self._ip_notify(
            _('Rates refreshed'),
            _('Fetched live India Post rates for %s shipment(s).') % len(self),
        )

    # ------------------------------------------------------------------
    # Address helpers
    # ------------------------------------------------------------------
    def _ip_origin_pincode(self):
        """Where India Post collects: the seller's own address."""
        self.ensure_one()
        seller = self.seller_id
        candidates = [
            self.shipping_from_zip,
            seller.zip if seller else None,
            seller.partner_id.zip if seller and seller.partner_id else None,
        ]
        for candidate in candidates:
            if (candidate or '').strip():
                return ipc.normalize_pincode(candidate, _('Seller pickup pincode'))
        raise ipc.IndiapostDataError(
            'The seller has no pickup pincode, so India Post cannot collect '
            'this shipment. Add a 6-digit pincode to the seller profile.'
        )

    def _ip_locality(self, pincode, fallback_city='', fallback_state=''):
        """Best available (city, state) pair for a pincode.

        India Post's own naming is preferred because that is what the booking
        and label endpoints expect to see; the Odoo district/state is the
        fallback when the office cache is cold and the API is unreachable.
        """
        Office = self.env['logistics.indiapost.office'].sudo()
        office = Office.resolve_booking_office(pincode, raise_if_missing=False)
        city = (office.city_name or office.taluk_name or '').strip()
        state = (office.state_name or '').strip()
        return (city or (fallback_city or '').strip(),
                state or (fallback_state or '').strip())

    def _ip_sender_group(self, settings):
        """KeralaXpress as the single consignor of record."""
        address = ipc.split_address_lines(
            settings['indiapost_sender_address'], _('Consignor address'))
        payload = {
            'sender_name': ipc.normalize_text(
                settings['indiapost_sender_name'], _('Consignor name')),
            'sender_company': ipc.normalize_text(
                settings['indiapost_sender_company']
                or settings['indiapost_sender_name'],
                _('Consignor company')),
            'sender_add_line_1': address[0],
            'sender_city': ipc.normalize_text(
                settings['indiapost_sender_city'], _('Consignor city')),
            'sender_state': ipc.normalize_text(
                settings['indiapost_sender_state'], _('Consignor state')),
            'sender_pincode': ipc.normalize_pincode(
                settings['indiapost_sender_pincode'], _('Consignor pincode')),
            'sender_mobile_no': ipc.normalize_mobile(
                settings['indiapost_sender_mobile'], _('Consignor mobile')),
        }
        if address[1]:
            payload['sender_add_line_2'] = address[1]
        if address[2]:
            payload['sender_add_line_3'] = address[2]
        if settings['indiapost_sender_email']:
            payload['sender_emailid'] = ipc.normalize_text(
                settings['indiapost_sender_email'], _('Consignor email'))
        if settings['indiapost_sender_gstin']:
            payload['sender_tax_reference'] = ipc.normalize_text(
                settings['indiapost_sender_gstin'], _('Consignor GSTIN'))
        return payload

    def _ip_receiver_group(self):
        self.ensure_one()
        pincode = ipc.normalize_pincode(
            self.shipping_to_zip, _('Customer pincode'))
        city, state = self._ip_locality(
            pincode,
            fallback_city=self.shipping_to_district_id.name,
            fallback_state=self.shipping_to_state_id.name,
        )
        address = ipc.split_address_lines(
            self.shipping_to_address, _('Customer address'))
        name = ipc.normalize_text(self.shipping_to_name, _('Customer name'))
        payload = {
            'receiver_name': name,
            # receiver_company is mandatory but retail customers have none, so
            # the addressee name is repeated. India Post prints both lines.
            'receiver_company': name,
            'receiver_add_line_1': address[0],
            'receiver_city': ipc.normalize_text(city, _('Customer city')),
            'receiver_state': ipc.normalize_text(state, _('Customer state')),
            'receiver_pincode': pincode,
            'receiver_mobile_no': ipc.normalize_mobile(
                self.shipping_to_mobile, _('Customer mobile')),
        }
        if address[1]:
            payload['receiver_add_line_2'] = address[1]
        if address[2]:
            payload['receiver_add_line_3'] = address[2]
        if self.shipping_to_email:
            payload['receiver_emailid'] = ipc.normalize_text(
                self.shipping_to_email, _('Customer email'), required=False)
        return payload

    def _ip_seller_address_parts(self):
        """The seller's own address, used for both pickup and returns."""
        self.ensure_one()
        seller = self.seller_id
        if not seller:
            raise ipc.IndiapostDataError(
                'This shipment has no seller, so India Post has nowhere to '
                'collect from.'
            )
        pincode = self._ip_origin_pincode()
        raw_address = self.shipping_from_address or '\n'.join(
            part for part in (seller.street, seller.street2) if part)
        address = ipc.split_address_lines(
            raw_address, _('Seller pickup address'))
        city, state = self._ip_locality(
            pincode,
            fallback_city=seller.city or seller.district_id.name,
            fallback_state=seller.state_id.name,
        )
        name = ipc.normalize_text(
            self.shipping_from_name or seller.name, _('Seller name'))
        return {
            'name': name,
            'company': name,
            'address': address,
            'city': ipc.normalize_text(city, _('Seller city')),
            'state': ipc.normalize_text(state, _('Seller state')),
            'pincode': pincode,
            'mobile': ipc.normalize_mobile(
                seller.phone or seller.partner_id.phone,
                _('Seller mobile number')),
        }

    def _ip_pickup_group(self, parts):
        """India Post collects from the seller, in one of two fixed slots."""
        self.ensure_one()
        pickup_date = self.indiapost_pickup_date or self._ip_default_pickup_date()
        slot = self.indiapost_pickup_slot or ipc.PICKUP_SLOTS[0][0]
        moment = ipc.pickup_datetime(pickup_date, slot)
        payload = {
            'pickup_address_flag': 'TRUE',
            'pickup_addressee_name': parts['name'],
            'pickup_company_name': parts['company'],
            'pickup_address_line1': parts['address'][0],
            'pickup_city': parts['city'],
            'pickup_state': parts['state'],
            'pickup_pincode': parts['pincode'],
            'pickup_mobile_no': parts['mobile'],
            'pickup_schedule_slot': slot,
            'pickup_schedule_date': ipc.format_pickup_datetime(moment),
        }
        if parts['address'][1]:
            payload['pickup_address_line2'] = parts['address'][1]
        if parts['address'][2]:
            payload['pickup_address_line3'] = parts['address'][2]
        return payload

    def _ip_alt_group(self, parts):
        """Returns go to the seller, not to KeralaXpress.

        Without ``alt_address_flag`` every undelivered article would come back
        to the consignor of record, which is us.
        """
        payload = {
            'alt_address_flag': 'TRUE',
            'alt_addressee_name': parts['name'],
            'alt_company_name': parts['company'],
            'alt_address_line1': parts['address'][0],
            'alt_city': parts['city'],
            'alt_state': parts['state'],
            'alt_pincode': parts['pincode'],
            'alt_alternate_mobile_no': parts['mobile'],
        }
        if parts['address'][1]:
            payload['alt_address_line2'] = parts['address'][1]
        return payload

    # ------------------------------------------------------------------
    # Payload
    # ------------------------------------------------------------------
    def _ip_prepare_article(self, settings, barcode):
        """Build one entry of the ``articles`` array."""
        self.ensure_one()
        grams = ipc.kg_to_grams(self.total_weight)
        errors, _warnings = ipc.validate_package(
            grams, self.length_cm, self.breadth_cm, self.height_cm)
        if errors:
            raise ipc.IndiapostDataError('\n'.join(errors))

        origin_pincode = self._ip_origin_pincode()
        office = self.env['logistics.indiapost.office'].sudo() \
            .resolve_booking_office(origin_pincode)
        seller_parts = self._ip_seller_address_parts()

        article = {
            'bulk_customer_id': settings['indiapost_customer_id'],
            'contract_id': settings['indiapost_contract_id'],
            'barcode_no': barcode,
            'pickup_or_dropoff': 'PICKUP',
            'pickup_dropoff_office_id': office.office_id,
            'article_type': ipc.ARTICLE_TYPE_SPEED_POST,
            # Must be a whole number of grams: 1500.5 is rejected with
            # "Physical weight must be a whole number".
            'physical_weight': grams,
            'shape_of_article': ipc.resolve_shape(
                grams, cylindrical=self.is_cylindrical),
            'length': ipc.cm_to_int(self.length_cm),
            'breadth_diameter': ipc.cm_to_int(self.breadth_cm),
            'height': ipc.cm_to_int(self.height_cm),
            # Our own AWB, so a booking can always be traced back even if the
            # response echoes identifiers we did not expect.
            'bulk_reference': (self.name or '')[:ipc.BULK_REFERENCE_MAX_LEN],
        }
        article.update(self._ip_sender_group(settings))
        article.update(self._ip_receiver_group())
        article.update(self._ip_pickup_group(seller_parts))
        article.update(self._ip_alt_group(seller_parts))

        if self.order_payment_type == 'cod' and self.cod_amount > 0:
            article['codr_cod'] = 'COD'
            article['value_for_codr_cod'] = round(self.cod_amount, 2)
        if self.indiapost_insurance_value > 0:
            article['insurance_type'] = 'DOP'
            article['value_of_insurance'] = round(self.indiapost_insurance_value, 2)
        for flag, key in (('indiapost_vas_ack', 'ack'),
                          ('indiapost_vas_reg', 'reg'),
                          ('indiapost_vas_otp', 'otp')):
            if self[flag]:
                article[key] = 'TRUE'
        return article

    def _ip_bookable(self):
        """The subset of ``self`` that should be sent to India Post."""
        return self.filtered(lambda s: (
            s.fulfilment_method == 'indiapost'
            and s.indiapost_booking_state != 'booked'
            and not s.indiapost_article_number
            and s.state != 'cancelled'
        ))

    # ------------------------------------------------------------------
    # Booking
    # ------------------------------------------------------------------
    def action_indiapost_book(self):
        """Book every eligible shipment in ``self`` with India Post.

        Idempotent: a shipment that already carries an article number is
        skipped, and a shipment keeps the barcode it was first allocated, so a
        retry after a rejection re-sends the same number instead of burning
        another one.
        """
        settings = self.env['logistics.indiapost.client']._ip_require_configured()
        self._ip_check_booking_settings(settings)

        candidates = self._ip_bookable()
        already = self.filtered(lambda s: s.indiapost_article_number)
        skipped = self - candidates - already
        if not candidates:
            return self._ip_notify(
                _('Nothing to book'),
                _('%(booked)s shipment(s) are already booked and %(skipped)s '
                  'are not eligible for India Post.')
                % {'booked': len(already), 'skipped': len(skipped)},
                kind='warning',
            )

        booked_count = 0
        failed = []
        for chunk_start in range(0, len(candidates), BOOKING_CHUNK_SIZE):
            chunk = candidates[chunk_start:chunk_start + BOOKING_CHUNK_SIZE]
            booked, errors = chunk._ip_book_chunk(settings)
            booked_count += booked
            failed.extend(errors)

        title = _('India Post booking complete') if not failed \
            else _('India Post booking finished with errors')
        message = _('%s shipment(s) booked.') % booked_count
        if failed:
            message += '\n\n' + '\n'.join(failed[:10])
            if len(failed) > 10:
                message += '\n' + _('… and %s more.') % (len(failed) - 10)
        return self._ip_notify(
            title, message, kind='danger' if failed else 'success',
            sticky=bool(failed),
        )

    def _ip_check_booking_settings(self, settings):
        customer_id = (settings['indiapost_customer_id'] or '').strip()
        contract_id = (settings['indiapost_contract_id'] or '').strip()
        if not ipc.BULK_CUSTOMER_ID_RE.match(customer_id):
            raise UserError(_(
                'The India Post bulk customer id must be exactly 10 digits. '
                'Set it under Settings > Logistics > India Post.'
            ))
        if not ipc.CONTRACT_ID_RE.match(contract_id):
            raise UserError(_(
                'The India Post contract id must be exactly 8 digits. Set it '
                'under Settings > Logistics > India Post.\n\n'
                'Every booking is rejected without a valid Speed Post '
                'contract, so this cannot be left blank.'
            ))

    def _ip_book_chunk(self, settings):
        """Submit one chunk and write the results back. Returns (booked, errors)."""
        articles = []
        prepared = []
        errors = []
        for shipment in self:
            try:
                barcode = shipment._ip_reserve_barcode()
                articles.append(shipment._ip_prepare_article(
                    settings, barcode.barcode))
                prepared.append(shipment)
            except ipc.IndiapostDataError as exc:
                message = str(exc)
                shipment._ip_record_booking_error(message)
                errors.append('%s: %s' % (shipment.name, message))
            except (UserError, ValidationError) as exc:
                message = exc.args[0] if exc.args else str(exc)
                shipment._ip_record_booking_error(message)
                errors.append('%s: %s' % (shipment.name, message))

        if not articles:
            return 0, errors

        try:
            response = self.env['logistics.indiapost.client'].call(
                'POST',
                BOOKING_PATH_TEMPLATE % settings['indiapost_customer_id'],
                body={'articles': articles},
                operation='booking',
                order=self[0].order_id if self[0].order_id else None,
                settings=settings,
            )
        except IndiapostApiError as exc:
            # A transport or payload-level failure means nothing was booked.
            # Recording it per shipment (rather than raising) keeps the barcode
            # reservations and the diagnostics on the records.
            message = exc.user_message()
            for shipment in prepared:
                shipment._ip_record_booking_error(message)
            errors.append(_('India Post rejected the whole batch: %s') % message)
            return 0, errors

        booked = self._ip_apply_booking_response(response.payload or {}, prepared,
                                                 errors)
        return booked, errors

    def _ip_reserve_barcode(self):
        """The barcode for this shipment, allocating one on first use."""
        self.ensure_one()
        barcode = self.indiapost_barcode_id
        if not barcode:
            barcode = self.env['logistics.indiapost.barcode.range'].sudo().allocate(
                shipment=self)
            self.sudo().write({'indiapost_barcode_id': barcode.id})
        return barcode

    def _ip_apply_booking_response(self, payload, prepared, errors):
        """Distribute a bulk booking response over the shipments that made it.

        Two traps here. Per-article failures arrive as HTTP 200 with
        ``success: true`` and a populated ``error_articles``, so the status code
        proves nothing. And ``error_articles`` comes back out of order (indices
        0, 2, 1 were observed), so the position in the list cannot be trusted
        either — only ``index`` can.
        """
        batch_id = payload.get('batch_id') or ''
        correlation_id = payload.get('correlation_id') or ''
        mail_booking_id = payload.get('mail_booking_dom_id') or ''
        now = fields.Datetime.now()
        booked = 0

        results = {}
        for entry in (payload.get('valid_articles') or []):
            results[self._ip_result_index(entry, prepared)] = ('valid', entry)
        for entry in (payload.get('error_articles') or []):
            results.setdefault(
                self._ip_result_index(entry, prepared), ('error', entry))

        for position, shipment in enumerate(prepared):
            outcome = results.get(position)
            if not outcome:
                message = _(
                    'India Post did not report a result for this article. '
                    'Check the API log (batch %s) before retrying, so it is '
                    'not booked twice.'
                ) % (batch_id or '-')
                shipment._ip_record_booking_error(message, batch_id=batch_id,
                                                  correlation_id=correlation_id)
                errors.append('%s: %s' % (shipment.name, message))
                continue
            kind, entry = outcome
            if kind == 'valid':
                shipment._ip_record_booking_success(
                    entry, batch_id=batch_id, correlation_id=correlation_id,
                    mail_booking_id=mail_booking_id, booked_on=now)
                booked += 1
            else:
                message = '; '.join(str(item) for item in (entry.get('errors') or [])) \
                    or _('India Post rejected this article without saying why.')
                shipment._ip_record_booking_error(
                    message, batch_id=batch_id, correlation_id=correlation_id)
                errors.append('%s: %s' % (shipment.name, message))
        return booked

    @staticmethod
    def _ip_result_index(entry, prepared):
        """Position in the submitted array that a result entry refers to."""
        index = entry.get('index')
        if isinstance(index, int) and 0 <= index < len(prepared):
            return index
        # Fall back to our own reference, then to the barcode. Worth having:
        # when barcode_no was omitted the response echoed bulk_reference in the
        # barcode_no field, so neither key is reliable on its own.
        reference = entry.get('bulk_reference') or entry.get('barcode_no')
        for position, shipment in enumerate(prepared):
            if reference and reference in (shipment.name,
                                           shipment.indiapost_barcode_id.barcode):
                return position
        return -1

    def _ip_record_booking_success(self, entry, batch_id='', correlation_id='',
                                   mail_booking_id='', booked_on=None):
        self.ensure_one()
        barcode = entry.get('barcode_no') or self.indiapost_barcode_id.barcode
        self.sudo().write({
            'indiapost_article_number': barcode,
            'indiapost_booking_state': 'booked',
            'indiapost_batch_id': batch_id,
            'indiapost_correlation_id': correlation_id,
            'indiapost_mail_booking_dom_id': mail_booking_id,
            'indiapost_offset_number': str(entry.get('offset_number') or ''),
            'indiapost_block_number': str(entry.get('block_number') or ''),
            'indiapost_calculated_tariff': ipc.as_amount(
                entry.get('calculated_tariff')),
            'indiapost_booked_on': booked_on or fields.Datetime.now(),
            'indiapost_booking_error': False,
        })
        if self.indiapost_barcode_id:
            self.indiapost_barcode_id.sudo()._ip_mark_booked()
        self._create_custody_event(
            'indiapost_booked',
            note=_('India Post article %s booked (batch %s).')
            % (barcode, batch_id or '-'),
        )
        self.message_post(body=_(
            'Booked with India Post as article <strong>%(awb)s</strong> '
            '(batch %(batch)s, tariff %(tariff)s).'
        ) % {
            'awb': barcode,
            'batch': batch_id or '-',
            'tariff': ipc.as_amount(entry.get('calculated_tariff')),
        })

    def _ip_record_booking_error(self, message, batch_id='', correlation_id=''):
        self.ensure_one()
        self.sudo().write({
            'indiapost_booking_state': 'error',
            'indiapost_booking_error': message,
            'indiapost_batch_id': batch_id or self.indiapost_batch_id,
            'indiapost_correlation_id': (correlation_id
                                         or self.indiapost_correlation_id),
        })
        if self.indiapost_barcode_id:
            self.indiapost_barcode_id.sudo()._ip_mark_rejected(message)
        _logger.warning('India Post booking failed for %s: %s',
                        self.name, message)

    # ------------------------------------------------------------------
    # Label
    # ------------------------------------------------------------------
    def _ip_label_payload(self, settings):
        self.ensure_one()
        receiver = self._ip_receiver_group()
        sender = self._ip_sender_group(settings)
        size = self.indiapost_label_size or settings['indiapost_label_size'] or 'A6'
        payload = {
            'customer_id': int(settings['indiapost_customer_id']),
            'user_id': int(settings['indiapost_customer_id']),
            'channel_type': 'E',
            'user_type': 'R',
            'barcode_no': self.indiapost_article_number,
            'service_type': ipc.ARTICLE_TYPE_SPEED_POST,
            'booking_type': 'COMMERCIAL',
            'recipient_name': receiver['receiver_name'],
            'recipient_addressl1': receiver['receiver_add_line_1'],
            'recipient_city': receiver['receiver_city'],
            'recipient_state': receiver['receiver_state'],
            'recipient_pin': receiver['receiver_pincode'],
            'recipient_mobile': receiver['receiver_mobile_no'],
            'sender_name': sender['sender_name'],
            'sender_addressl1': sender['sender_add_line_1'],
            'sender_city': sender['sender_city'],
            'sender_state': sender['sender_state'],
            'sender_pin': sender['sender_pincode'],
            'sender_mobile': sender['sender_mobile_no'],
            'transmission_mode': settings['indiapost_transmission_mode'] or 'S',
            'payment_mode': 'CO',
            'payment_status': 'PC',
            'identifier': 'Domestic',
            'booking_office_name': ipc.normalize_text(
                settings['indiapost_booking_office_name']
                or self.indiapost_pickup_office_name
                or sender['sender_city'],
                _('Booking office name')),
            'booking_office_pin': ipc.normalize_pincode(
                settings['indiapost_booking_office_pin']
                or sender['sender_pincode'], _('Booking office pincode')),
            'size': size,
            'physical_weight': ipc.kg_to_grams(self.total_weight),
            'charged_weight': self.chargeable_weight_g or ipc.kg_to_grams(
                self.total_weight),
            'volumetric_weight': self.volumetric_weight_g,
            'article_length': str(ipc.cm_to_int(self.length_cm)),
            'article_breadth': str(ipc.cm_to_int(self.breadth_cm)),
            'article_height': str(ipc.cm_to_int(self.height_cm)),
            'destination_pin': receiver['receiver_pincode'],
            'insurance_flag': bool(self.indiapost_insurance_value),
            'insurance_value': round(self.indiapost_insurance_value, 2),
            'bkg_ref_id': (self.name or '')[:ipc.BULK_REFERENCE_MAX_LEN],
            'total_amount': round(
                self.indiapost_calculated_tariff or self.indiapost_total_tariff, 2),
            'priority': False,
            'registered_flag': bool(self.indiapost_vas_reg),
        }
        if self.indiapost_booked_on:
            payload['booking_datetime'] = self.indiapost_booked_on.strftime(
                '%d-%m-%Y %H:%M:%S')
        return payload

    def action_indiapost_fetch_label(self):
        """Download and store the India Post address label for each shipment.

        Label generation validates nothing and is completely decoupled from
        booking — it happily renders a barcode that was never booked, on an
        account with no contract. So correctness of the barcode is entirely on
        us, and we only ever request labels for articles we actually booked.
        """
        settings = self.env['logistics.indiapost.client']._ip_require_configured()
        Client = self.env['logistics.indiapost.client']
        fetched = 0
        problems = []
        for shipment in self:
            if not shipment.indiapost_article_number:
                problems.append(_('%s has no India Post article number yet.')
                                % shipment.name)
                continue
            try:
                payload = shipment._ip_label_payload(settings)
            except ipc.IndiapostDataError as exc:
                problems.append('%s: %s' % (shipment.name, exc))
                continue
            try:
                response = Client.call(
                    'POST', LABEL_PATH,
                    # The endpoint takes a JSON array, not an object.
                    body=[payload],
                    operation='label', shipment=shipment, expect_pdf=True,
                    settings=settings,
                )
            except IndiapostApiError as exc:
                problems.append('%s: %s' % (shipment.name, exc.user_message()))
                continue
            if not response.is_pdf or not response.content:
                problems.append(_('%s: India Post did not return a PDF.')
                                % shipment.name)
                continue
            shipment.sudo().write({
                'indiapost_label_pdf': base64.b64encode(response.content),
                'indiapost_label_filename': '%s.pdf'
                % shipment.indiapost_article_number,
                'indiapost_label_size': payload['size'],
                'indiapost_label_fetched_on': fields.Datetime.now(),
            })
            fetched += 1

        message = _('%s label(s) downloaded.') % fetched
        if problems:
            message += '\n\n' + '\n'.join(problems[:10])
        return self._ip_notify(
            _('India Post labels'), message,
            kind='danger' if problems else 'success', sticky=bool(problems),
        )

    def action_indiapost_open_label(self):
        """Open the stored label PDF in a new tab."""
        self.ensure_one()
        if not self.indiapost_label_pdf:
            raise UserError(_(
                'No India Post label is stored for %s yet. Use "Fetch India '
                'Post Label" first.'
            ) % self.name)
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/logistics.shipment/%s/indiapost_label_pdf/%s'
                   '?download=true' % (self.id, self.indiapost_label_filename
                                       or 'label.pdf'),
            'target': 'new',
        }

    # ------------------------------------------------------------------
    # Tracking
    # ------------------------------------------------------------------
    def action_indiapost_sync_tracking(self):
        """Pull the latest India Post scans for these shipments now."""
        trackable = self.filtered(lambda s: s.indiapost_article_number)
        if not trackable:
            raise UserError(_(
                'None of the selected shipments have an India Post article '
                'number yet, so there is nothing to track.'
            ))
        return self.env['logistics.indiapost.tracking'].action_ip_sync_now(trackable)

    # ------------------------------------------------------------------
    # Escape hatch
    # ------------------------------------------------------------------
    def action_indiapost_switch_to_own_network(self):
        """Divert a shipment India Post refuses onto the KeralaXpress hubs.

        Needed in practice: an article that is too small and too heavy for
        Speed Post, or a booking blocked by a contract problem, still has to
        reach the customer. Only administrators can do this, and the reserved
        barcode is voided so it is never reused.
        """
        if not self.env.user.has_group('keralariders_logistics.group_logistics_admin'):
            raise UserError(_(
                'Only a Logistics Administrator can move a shipment onto the '
                'hub network.'
            ))
        for record in self:
            if record.indiapost_article_number:
                raise UserError(_(
                    'Shipment %s is already booked with India Post as article '
                    '%s. Cancel it with India Post before diverting it.'
                ) % (record.name, record.indiapost_article_number))
            barcode = record.indiapost_barcode_id
            # The admin check above is this write's authorisation, so it opts
            # in explicitly rather than relying on the group check firing twice.
            record.sudo().with_context(
                allow_fulfilment_method_write=True,
            ).write({
                'fulfilment_method': 'own_network',
                'indiapost_booking_state': 'not_required',
                'indiapost_booking_error': False,
                'indiapost_barcode_id': False,
            })
            if barcode:
                barcode.sudo().action_ip_void()
            # It is a KeralaXpress collection from now on, so it needs a pickup
            # executive — India Post shipments are deliberately kept out of
            # that assignment, and this is the moment the shipment stops being
            # one.
            record._auto_assign_pickup_executive()
            record.message_post(body=_(
                'Diverted to the KeralaXpress hub network; India Post booking '
                'abandoned.'
            ))
        # Charges revert to the slab table, so re-run the compute.
        self.invalidate_recordset(['delivery_charges_subtotal',
                                   'delivery_charges_total'])
        return self._ip_notify(
            _('Diverted to hub network'),
            _('%s shipment(s) will now be delivered by KeralaXpress.') % len(self),
        )

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------
    def _ip_notify(self, title, message, kind='success', sticky=False):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': title,
                'message': message,
                'type': kind,
                'sticky': sticky,
            },
        }
