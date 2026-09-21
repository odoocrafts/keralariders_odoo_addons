"""India Post booking and labelling on ``logistics.shipment``.

The hub network is untouched: a shipment whose seller is on ``own_network``
behaves exactly as before. Only ``fulfilment_method == 'indiapost'`` shipments
go anywhere near this code.

Address model for a booking, as decided by the business:
  * KeralaXpress remains the bulk customer / contract holder
    (``bulk_customer_id``, ``contract_id``) — those ids are not the pickup
    address;
  * ``sender_*`` is the physical from-address India Post prints as SENDER and
    collects from, so it is the seller (not the company warehouse);
  * pickup happens at the same seller premises (``pickup_address_flag``);
  * the alternate address is the seller, so undelivered articles come back to
    the seller and not to us (``alt_address_flag``).
Those are three independent address groups in one article, which the sandbox
validator accepts. The official CEPT label SENDER line follows ``sender_*``.
"""

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tools import html_escape
from odoo.tools.float_utils import float_compare
from markupsafe import Markup

import base64
import json
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
QUOTE_SIGNATURE_VERSION = 'v2'

# An India Post shipment is billed the postal tariff rather than the slab
# table, which makes the stored quote the price: these fields decide what the
# seller pays and are added to the delivery charge guard on logistics.shipment.
# The two staleness fields belong here as much as the amounts do — a seller who
# could backdate the signature would make a forged tariff look freshly quoted
# and walk straight past the re-quote that protects the debit.
INDIAPOST_QUOTE_FIELDS = (
    'indiapost_base_tariff',
    'indiapost_vas_charges',
    'indiapost_tax_amount',
    'indiapost_total_tariff',
    'indiapost_quote_signature',
    'indiapost_tariff_quoted_on',
)

# Original pickup debit vs India Post scan reweigh. Same guard as the quote:
# a portal write here would mint a wallet credit.
INDIAPOST_SCAN_ADJUST_FIELDS = (
    'indiapost_orig_charge',
    'indiapost_orig_weight_g',
    'indiapost_orig_length_cm',
    'indiapost_orig_breadth_cm',
    'indiapost_orig_height_cm',
    'indiapost_orig_volumetric_g',
    'indiapost_scan_weight_g',
    'indiapost_scan_length_cm',
    'indiapost_scan_breadth_cm',
    'indiapost_scan_height_cm',
    'indiapost_scan_volumetric_g',
    'indiapost_scan_chargeable_g',
    'indiapost_scan_tariff_raw',
    'indiapost_scan_tariff_trusted',
    'indiapost_scan_quote',
    'indiapost_scan_quote_source',
    'indiapost_scan_difference',
    'indiapost_scan_raw',
    'indiapost_scan_seen',
    'indiapost_scan_quote_pending',
    'indiapost_scan_adjusted',
    'indiapost_scan_notified',
    'indiapost_scan_wallet_txn_id',
)

INDIAPOST_CHARGE_FIELDS = INDIAPOST_QUOTE_FIELDS + INDIAPOST_SCAN_ADJUST_FIELDS

SCAN_ADJ_REF_PREFIX = 'IP-SCAN-ADJ:'


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

    indiapost_article_type = fields.Selection(
        ipc.ARTICLE_TYPES, string='India Post Product',
        default=ipc.ARTICLE_TYPE_SPEED_POST, required=True, copy=False,
        tracking=True,
        help='India Post prices, books and contracts each product separately, '
             'so this decides both the article type sent at booking and which '
             'of the two contract ids the booking is validated against.',
    )

    @api.depends('fulfilment_method')
    def _compute_is_indiapost(self):
        for record in self:
            record.is_indiapost = record.fulfilment_method == 'indiapost'

    def _ip_product(self):
        """This shipment's India Post product, defaulting to Speed Post."""
        self.ensure_one()
        return self.indiapost_article_type or ipc.ARTICLE_TYPE_SPEED_POST

    def _ip_contract_id(self, settings):
        """The contract id this shipment has to be booked against."""
        self.ensure_one()
        return self.env['logistics.indiapost.client']._ip_contract_id(
            settings, self._ip_product())

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
        help='What India Post bills: the greater of actual and volumetric '
             'weight (length x breadth x height / 5). Speed Post is quoted '
             'as inland parcel at every weight.',
    )
    indiapost_product_code = fields.Char(
        string='Speed Post Product Code', compute='_compute_indiapost_package',
        store=True,
        help='Speed Post inland parcel (SP_INLAND_PARCEL), including items '
             'below 501 g. Booking sends article_type=SP with shape NROL.',
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

    def portal_indiapost_arn(self):
        """Allocated India Post article (ARN) for seller-portal AWB cells.

        Empty unless this shipment is India Post fulfilment *and* booking has
        stored an article number. Hub/DE rows must not render a blank ARN
        line, including a leftover barcode after a divert to the hub network.
        """
        self.ensure_one()
        if self.fulfilment_method != 'indiapost':
            return False
        return (self.indiapost_article_number or '').strip() or False

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
    indiapost_booking_in_progress = fields.Boolean(
        string='India Post Booking In Progress', copy=False, default=False,
        help='Set while a pickup/book request is talking to India Post so a '
             'second click cannot submit the same article again.',
    )

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
            # A Business Parcel and a Speed Post article of identical size are
            # priced from different tariff tables, so the product is as much a
            # pricing input as the weight is.
            'prod%s' % self._ip_product(),
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
        except (UserError, ipc.IndiapostDataError):
            return ''

    @api.depends('fulfilment_method', 'indiapost_tariff_quoted_on',
                 'indiapost_quote_signature', 'indiapost_article_number',
                 'indiapost_booking_state', 'indiapost_article_type',
                 'total_weight', 'length_cm',
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
    indiapost_label_pdf = fields.Binary(
        string='India Post Label', attachment=True,
        copy=False, readonly=True, groups='base.group_user',
    )
    indiapost_label_filename = fields.Char(string='Label Filename', copy=False,
                                           readonly=True)
    indiapost_label_size = fields.Selection(
        [('A6', 'A6'), ('A7', 'A7')], string='Label Size', copy=False,
    )
    indiapost_label_fetched_on = fields.Datetime(string='Label Fetched On',
                                                 copy=False, readonly=True)
    indiapost_sort_code = fields.Char(
        string='India Post Sort Code', copy=False, readonly=True,
        help='Letter India Post prints in the destination PIN box of the CEPT '
             'label (transmission mode: S=Surface, A=Air). Taken from the '
             'stored label PDF when possible, otherwise the transmission_mode '
             'sent on label create. Never invent a value.',
    )

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
    # Scan reweigh / volumetric adjustment
    #
    # Wallet is debited at Request Pickup from the seller's declared
    # package. India Post may later reweigh or remeasure. Bulk tracking
    # ``booking_details`` has a ``tariff`` field (often 0) and no
    # weight/dimensions in every payload we have seen. When actuals appear
    # we re-quote through the tariff endpoint; ops can enter them by hand.
    # Webhook amounts are stored for audit and never posted to the wallet
    # on their own — same rule as ignoring webhook charge keys.
    # ------------------------------------------------------------------
    indiapost_orig_charge = fields.Monetary(
        string='Charged at Pickup', currency_field='currency_id',
        copy=False, readonly=True,
        help='Delivery charge taken from the seller wallet at Request Pickup. '
             'Scan adjustments are the difference against this amount.',
    )
    indiapost_orig_weight_g = fields.Integer(
        string='Original Weight (g)', copy=False, readonly=True)
    indiapost_orig_length_cm = fields.Integer(
        string='Original Length (cm)', copy=False, readonly=True)
    indiapost_orig_breadth_cm = fields.Integer(
        string='Original Breadth (cm)', copy=False, readonly=True)
    indiapost_orig_height_cm = fields.Integer(
        string='Original Height (cm)', copy=False, readonly=True)
    indiapost_orig_volumetric_g = fields.Integer(
        string='Original Volumetric (g)', copy=False, readonly=True)
    indiapost_scan_weight_g = fields.Integer(
        string='India Post Actual Weight (g)', copy=False)
    indiapost_scan_length_cm = fields.Integer(
        string='India Post Actual Length (cm)', copy=False)
    indiapost_scan_breadth_cm = fields.Integer(
        string='India Post Actual Breadth (cm)', copy=False)
    indiapost_scan_height_cm = fields.Integer(
        string='India Post Actual Height (cm)', copy=False)
    indiapost_scan_volumetric_g = fields.Integer(
        string='India Post Volumetric (g)', copy=False)
    indiapost_scan_chargeable_g = fields.Integer(
        string='India Post Chargeable (g)', copy=False)
    indiapost_scan_tariff_raw = fields.Monetary(
        string='India Post Reported Tariff', currency_field='currency_id',
        copy=False, readonly=True,
        help='booking_details.tariff (or equivalent) when the API sends a '
             'non-zero figure. Often 0 even after scans.',
    )
    indiapost_scan_tariff_trusted = fields.Boolean(
        string='Reported Tariff from Tracking Poll', copy=False, readonly=True,
        help='True when the reported tariff came from our authenticated bulk '
             'tracking poll, not an inbound webhook.',
    )
    indiapost_scan_quote = fields.Monetary(
        string='Quote after Scan', currency_field='currency_id', copy=False)
    indiapost_scan_quote_source = fields.Selection(
        [
            ('tariff', 'Re-quoted from actuals'),
            ('booking_tariff', 'India Post tracking tariff'),
            ('ops', 'Entered by KeralaXpress'),
        ],
        string='Scan Quote Source', copy=False, readonly=True,
    )
    indiapost_scan_difference = fields.Monetary(
        string='Scan Adjustment', currency_field='currency_id',
        copy=False, readonly=True,
        help='Scan quote minus the pickup debit. Positive means the seller '
             'owes more (wallet debit); negative is a credit.',
    )
    indiapost_scan_raw = fields.Text(
        string='India Post Scan Actuals (raw)', copy=False, readonly=True)
    indiapost_scan_seen = fields.Boolean(
        string='India Post Scan Actuals Recorded', copy=False, readonly=True,
        help='Something usable (weight, dimensions, or a reported tariff) '
             'arrived after scan. The seller portal shows the comparison '
             'only when this is set.',
    )
    indiapost_scan_quote_pending = fields.Boolean(
        string='Scan Re-quote Pending', copy=False, readonly=True, index=True,
        help='Actuals are stored but a tariff HTTP call was skipped (webhook) '
             'or failed. The tracking cron finishes it.',
    )
    indiapost_scan_adjusted = fields.Boolean(
        string='Scan Adjustment Applied', copy=False, readonly=True, index=True,
        help='The scan quote has been evaluated once. A wallet line is posted '
             'only when the difference is non-zero.',
    )
    indiapost_scan_notified = fields.Boolean(
        string='Scan Adjustment Emailed', copy=False, readonly=True)
    indiapost_scan_wallet_txn_id = fields.Many2one(
        'logistics.wallet.transaction', string='Scan Adjustment Wallet Line',
        copy=False, readonly=True, ondelete='set null',
    )

    def portal_indiapost_scan_visible(self):
        """Seller portal shows the original vs scan block when we have actuals."""
        self.ensure_one()
        return bool(
            self.fulfilment_method == 'indiapost'
            and not self.is_return_journey
            and self.indiapost_scan_seen
        )

    def _ip_scan_adj_reference(self):
        self.ensure_one()
        return '%s%s' % (SCAN_ADJ_REF_PREFIX, self.id)

    def _ip_scan_adj_label(self):
        self.ensure_one()
        awb = self.name or ''
        arn = (self.indiapost_article_number or '').strip()
        if arn and arn != awb:
            return _('India Post rate adjustment — AWB %s (ARN %s)') % (
                awb, arn)
        return _('India Post rate adjustment — AWB %s') % awb

    def _ip_snapshot_charged_package(self):
        """Freeze the declared package and pickup debit for later comparison."""
        for record in self:
            if record.fulfilment_method != 'indiapost':
                continue
            if record.indiapost_orig_charge:
                continue
            charge = 0.0
            if record.wallet_transaction_id:
                charge = abs(record.wallet_transaction_id.amount or 0.0)
            if not charge:
                charge = abs(record.delivery_charges_total or 0.0)
            if not charge:
                continue
            length = ipc.cm_to_int(record.length_cm)
            breadth = ipc.cm_to_int(record.breadth_cm)
            height = ipc.cm_to_int(record.height_cm)
            record.with_context(allow_delivery_charge_write=True).write({
                'indiapost_orig_charge': record._round_charge(charge),
                'indiapost_orig_weight_g': ipc.kg_to_grams(record.total_weight),
                'indiapost_orig_length_cm': length,
                'indiapost_orig_breadth_cm': breadth,
                'indiapost_orig_height_cm': height,
                'indiapost_orig_volumetric_g': ipc.volumetric_weight_g(
                    length, breadth, height),
            })

    def _ip_scan_eligible(self):
        self.ensure_one()
        if self.fulfilment_method != 'indiapost':
            return False
        if self.is_return_journey:
            return False
        if not self.wallet_transaction_id:
            return False
        if abs(self.wallet_transaction_id.amount or 0.0) < 0.005:
            return False
        return True

    def _ip_existing_scan_wallet_line(self):
        self.ensure_one()
        if self.indiapost_scan_wallet_txn_id:
            return self.indiapost_scan_wallet_txn_id
        return self.env['logistics.wallet.transaction'].sudo().search([
            ('shipment_id', '=', self.id),
            ('reference', '=', self._ip_scan_adj_reference()),
        ], limit=1)

    def _ip_merge_scan_actuals(self, extracted):
        """Write newly observed actuals without wiping values we already have."""
        self.ensure_one()
        extracted = extracted or {}
        vals = {}
        mapping = (
            ('weight_g', 'indiapost_scan_weight_g'),
            ('length_cm', 'indiapost_scan_length_cm'),
            ('breadth_cm', 'indiapost_scan_breadth_cm'),
            ('height_cm', 'indiapost_scan_height_cm'),
            ('volumetric_g', 'indiapost_scan_volumetric_g'),
            ('chargeable_g', 'indiapost_scan_chargeable_g'),
        )
        for src, dest in mapping:
            value = int(extracted.get(src) or 0)
            if value > 0 and value != (self[dest] or 0):
                vals[dest] = value
        tariff = extracted.get('tariff')
        if tariff not in (None, False, '') and ipc.as_amount(tariff) > 0:
            amount = self._round_charge(ipc.as_amount(tariff))
            if float_compare(amount, self.indiapost_scan_tariff_raw or 0.0,
                             precision_digits=2) != 0:
                vals['indiapost_scan_tariff_raw'] = amount
            if extracted.get('tariff_trusted') and not self.indiapost_scan_tariff_trusted:
                vals['indiapost_scan_tariff_trusted'] = True
        raw_bits = extracted.get('raw')
        if raw_bits:
            dumped = json.dumps(raw_bits, default=str, sort_keys=True)[:8000]
            if dumped != (self.indiapost_scan_raw or ''):
                vals['indiapost_scan_raw'] = dumped
        length = vals.get('indiapost_scan_length_cm', self.indiapost_scan_length_cm)
        breadth = vals.get('indiapost_scan_breadth_cm', self.indiapost_scan_breadth_cm)
        height = vals.get('indiapost_scan_height_cm', self.indiapost_scan_height_cm)
        if length and breadth and height and not (
                vals.get('indiapost_scan_volumetric_g')
                or self.indiapost_scan_volumetric_g):
            vals['indiapost_scan_volumetric_g'] = ipc.volumetric_weight_g(
                length, breadth, height)
        has_actuals = bool(
            (vals.get('indiapost_scan_weight_g') or self.indiapost_scan_weight_g)
            or (vals.get('indiapost_scan_length_cm') or self.indiapost_scan_length_cm)
            or (vals.get('indiapost_scan_breadth_cm') or self.indiapost_scan_breadth_cm)
            or (vals.get('indiapost_scan_height_cm') or self.indiapost_scan_height_cm)
            or (vals.get('indiapost_scan_volumetric_g') or self.indiapost_scan_volumetric_g)
            or (vals.get('indiapost_scan_tariff_raw') or self.indiapost_scan_tariff_raw)
        )
        if has_actuals and not self.indiapost_scan_seen:
            vals['indiapost_scan_seen'] = True
        if vals:
            self.with_context(allow_delivery_charge_write=True).write(vals)
        return has_actuals or self.indiapost_scan_seen

    def _ip_scan_package_changed(self):
        """True when India Post actual weight or dims differ from pickup."""
        self.ensure_one()
        if self.indiapost_scan_weight_g and self.indiapost_orig_weight_g:
            if self.indiapost_scan_weight_g != self.indiapost_orig_weight_g:
                return True
        orig = (
            self.indiapost_orig_length_cm,
            self.indiapost_orig_breadth_cm,
            self.indiapost_orig_height_cm,
        )
        scan = (
            self.indiapost_scan_length_cm,
            self.indiapost_scan_breadth_cm,
            self.indiapost_scan_height_cm,
        )
        if any(scan) and scan != orig:
            return True
        if (self.indiapost_scan_volumetric_g
                and self.indiapost_orig_volumetric_g
                and self.indiapost_scan_volumetric_g
                != self.indiapost_orig_volumetric_g):
            return True
        return False

    def _ip_scan_has_dims(self):
        self.ensure_one()
        return bool(
            self.indiapost_scan_length_cm
            and self.indiapost_scan_breadth_cm
            and self.indiapost_scan_height_cm
        )

    def _ip_markup_amount(self, base, settings=None):
        settings = settings or self.env['logistics.indiapost.client']._ip_settings()
        percent = settings.get('indiapost_quote_markup_percent') or 0.0
        return round(ipc.as_amount(base) * percent / 100.0, 2)

    def _ip_cached_scan_quote(self, weight_g, length, breadth, height,
                              settings=None):
        """Tariff cache only — never calls India Post."""
        self.ensure_one()
        settings = settings or self.env['logistics.indiapost.client']._ip_settings()
        if not settings.get('indiapost_enabled'):
            return None
        Tariff = self.env['logistics.indiapost.tariff']
        billed_g = ipc.band_weight(weight_g)
        vas_key = Tariff._ip_vas_key(
            insurance_value=self.indiapost_insurance_value,
            **self._ip_vas_flags())
        origin = self._ip_origin_pincode_soft()
        dest = (self.shipping_to_zip or '').strip()
        if not origin or not dest:
            return None
        try:
            dest = ipc.normalize_pincode(dest, _('Destination pincode'))
        except ipc.IndiapostDataError:
            return None
        cache_key = Tariff._ip_cache_key(
            origin, dest, billed_g, length, breadth, height, vas_key,
            settings['indiapost_environment'], article_type=self._ip_product(),
        )
        entry = self.env['logistics.indiapost.tariff.cache'].sudo().search([
            ('cache_key', '=', cache_key),
            ('expires_at', '>', fields.Datetime.now()),
        ], limit=1)
        if not entry:
            return None
        quote = Tariff._ip_quote_from_cache(entry)
        markup = self._ip_markup_amount(quote['final_amount'], settings)
        quote['total_payable'] = round(quote['final_amount'] + markup, 2)
        quote['billed_weight_g'] = billed_g
        quote['volumetric_weight_g'] = ipc.volumetric_weight_g(
            length, breadth, height)
        quote['chargeable_weight_g'] = quote.get('chargeable_weight_g') or max(
            billed_g, quote['volumetric_weight_g'])
        return quote

    def _ip_requote_scan_package(self, settings=None):
        """Live (or cached) tariff for the scanned weight and dimensions."""
        self.ensure_one()
        weight_g = self.indiapost_scan_weight_g or self.indiapost_orig_weight_g
        if not weight_g or not self._ip_scan_has_dims():
            return None
        settings = settings or self.env['logistics.indiapost.client']._ip_settings()
        origin = self._ip_origin_pincode_soft()
        dest = (self.shipping_to_zip or '').strip()
        if not origin or not dest:
            return None
        quote = self.env['logistics.indiapost.tariff'].quote_safe(
            origin, dest,
            article_type=self._ip_product(),
            weight_g=weight_g,
            length_cm=self.indiapost_scan_length_cm,
            breadth_cm=self.indiapost_scan_breadth_cm,
            height_cm=self.indiapost_scan_height_cm,
            insurance_value=self.indiapost_insurance_value,
            use_cache=True,
            shipment=self,
            settings=settings,
            **self._ip_vas_flags()
        )
        if not quote or not quote.get('ok'):
            return None
        return quote

    def _ip_resolve_scan_quote(self, extracted=None, allow_tariff_http=False,
                               allow_api_tariff=False):
        """Pick a scan quote without inventing India Post figures.

        Preference: tariff re-quote from actual weight/dims, then a trusted
        tracking ``tariff`` (poll only), then a figure ops typed in.
        """
        self.ensure_one()
        extracted = extracted or {}
        settings = self.env['logistics.indiapost.client']._ip_settings()
        if self._ip_scan_has_dims() and (
                self.indiapost_scan_weight_g or self.indiapost_orig_weight_g):
            if allow_tariff_http:
                quote = self._ip_requote_scan_package(settings=settings)
                if quote:
                    return quote['total_payable'], 'tariff', quote
            cached = self._ip_cached_scan_quote(
                self.indiapost_scan_weight_g or self.indiapost_orig_weight_g,
                self.indiapost_scan_length_cm,
                self.indiapost_scan_breadth_cm,
                self.indiapost_scan_height_cm,
                settings=settings,
            )
            if cached:
                return cached['total_payable'], 'tariff', cached
            if not allow_tariff_http:
                return None, 'pending', None
        if allow_api_tariff and self.indiapost_scan_tariff_trusted \
                and (self.indiapost_scan_tariff_raw or 0.0) > 0:
            raw = self.indiapost_scan_tariff_raw
            payable = self._round_charge(raw + self._ip_markup_amount(raw, settings))
            return payable, 'booking_tariff', None
        if extracted.get('ops_quote') not in (None, False, ''):
            return self._round_charge(ipc.as_amount(extracted['ops_quote'])), 'ops', None
        if self.indiapost_scan_quote and self.indiapost_scan_quote_source == 'ops':
            return self.indiapost_scan_quote, 'ops', None
        return None, 'pending' if self.indiapost_scan_seen else None, None

    def _ip_apply_scan_rate_adjustment(self, extracted=None,
                                       allow_tariff_http=False,
                                       allow_api_tariff=False):
        """Persist scan actuals, optionally re-quote, and post one wallet line.

        Idempotent. Safe to call from tracking polls, Sync Now, cron, and the
        ops button. Never raises to the caller: a tariff failure leaves the
        row pending instead of blocking a scan.
        """
        extracted = extracted or {}
        for record in self:
            try:
                record._ip_apply_scan_rate_adjustment_one(
                    extracted=extracted,
                    allow_tariff_http=allow_tariff_http,
                    allow_api_tariff=allow_api_tariff,
                )
            except Exception:
                _logger.exception(
                    'India Post scan rate adjustment failed for shipment %s',
                    record.name,
                )
        return True

    def _ip_apply_scan_rate_adjustment_one(self, extracted=None,
                                           allow_tariff_http=False,
                                           allow_api_tariff=False):
        self.ensure_one()
        if not self._ip_scan_eligible():
            return False
        self._ip_snapshot_charged_package()
        seen = self._ip_merge_scan_actuals(extracted)
        existing = self._ip_existing_scan_wallet_line()
        if self.indiapost_scan_adjusted and existing:
            return False
        if self.indiapost_scan_adjusted and not self.indiapost_scan_quote_pending:
            return False
        if not seen and not (extracted or {}).get('ops_quote'):
            return False

        payable, source, quote = self._ip_resolve_scan_quote(
            extracted=extracted,
            allow_tariff_http=allow_tariff_http,
            allow_api_tariff=allow_api_tariff,
        )
        vals = {}
        if source == 'pending' or payable is None:
            if self.indiapost_scan_seen and not self.indiapost_scan_quote_pending:
                vals['indiapost_scan_quote_pending'] = True
            if vals:
                self.with_context(allow_delivery_charge_write=True).write(vals)
            if self._ip_scan_package_changed() and not self.indiapost_scan_notified:
                self._ip_queue_scan_adjustment_mail(wallet_amount=0.0)
            return False

        payable = self._round_charge(payable)
        orig = self._round_charge(self.indiapost_orig_charge or 0.0)
        difference = self._round_charge(payable - orig)
        if quote:
            if quote.get('volumetric_weight_g') and not self.indiapost_scan_volumetric_g:
                vals['indiapost_scan_volumetric_g'] = int(quote['volumetric_weight_g'])
            if quote.get('chargeable_weight_g') and not self.indiapost_scan_chargeable_g:
                vals['indiapost_scan_chargeable_g'] = int(quote['chargeable_weight_g'])
        vals.update({
            'indiapost_scan_quote': payable,
            'indiapost_scan_quote_source': source,
            'indiapost_scan_difference': difference,
            'indiapost_scan_quote_pending': False,
            'indiapost_scan_seen': True,
        })
        self.with_context(allow_delivery_charge_write=True).write(vals)

        wallet_amount = 0.0
        posted_wallet = False
        if float_compare(difference, 0.0, precision_digits=2) != 0:
            if existing:
                wallet_amount = existing.amount
            else:
                wallet_amount = self._round_charge(-difference)
                posted = self._ip_post_scan_wallet_line(wallet_amount)
                if posted:
                    posted_wallet = True
                    existing = posted
        self.with_context(allow_delivery_charge_write=True).write({
            'indiapost_scan_adjusted': True,
            'indiapost_scan_wallet_txn_id': existing.id if existing else False,
        })

        package_changed = self._ip_scan_package_changed()
        should_mail = posted_wallet or (
            package_changed and not self.indiapost_scan_notified)
        if should_mail:
            self._ip_queue_scan_adjustment_mail(
                wallet_amount=wallet_amount if (
                    posted_wallet or existing) else 0.0)
        return True

    def _ip_post_scan_wallet_line(self, amount):
        """One credit (positive) or debit (negative) for the scan difference."""
        self.ensure_one()
        if float_compare(amount, 0.0, precision_digits=2) == 0:
            return self.env['logistics.wallet.transaction']
        if self._ip_existing_scan_wallet_line():
            return self._ip_existing_scan_wallet_line()
        wallet = (self.seller_id.wallet_ids[:1]
                  or self.wallet_transaction_id.wallet_id)
        if not wallet:
            _logger.warning(
                'India Post scan adjustment for %s skipped: no seller wallet',
                self.name,
            )
            return self.env['logistics.wallet.transaction']
        transaction = self.env['logistics.wallet.transaction'].sudo().create({
            'wallet_id': wallet.id,
            'amount': amount,
            'transaction_date': fields.Date.context_today(self),
            'shipment_id': self.id,
            'order_id': self.order_id.id if self.order_id else False,
            'reference': self._ip_scan_adj_reference(),
            'description': self._ip_scan_adj_label(),
        })
        self.sudo().message_post(body=_(
            'India Post rate adjustment of %(amount)s posted on the seller '
            'wallet (%(label)s).',
            amount=self._ip_format_money(amount),
            label=self._ip_scan_adj_label(),
        ))
        return transaction

    def _ip_format_money(self, amount):
        self.ensure_one()
        if self.currency_id:
            return self.currency_id.format(amount or 0.0)
        return '%.2f' % (amount or 0.0)

    def _ip_format_weight_g(self, grams):
        if not grams:
            return _('not reported')
        return _('%s g') % int(grams)

    def _ip_format_dims_cm(self, length, breadth, height):
        if not (length or breadth or height):
            return _('not reported')
        return _('%s × %s × %s cm') % (
            int(length or 0), int(breadth or 0), int(height or 0))

    def _ip_queue_scan_adjustment_mail(self, wallet_amount=0.0):
        """One queued seller mail covering package change and/or wallet move."""
        self.ensure_one()
        if self.indiapost_scan_notified and float_compare(
                wallet_amount, 0.0, precision_digits=2) == 0:
            return self.env['mail.mail']
        seller = self.seller_id
        email = ((seller.email or seller.partner_id.email or '') if seller else '').strip()
        if not email or '@' not in email:
            self.with_context(allow_delivery_charge_write=True).write({
                'indiapost_scan_notified': True,
            })
            return self.env['mail.mail']
        Mail = self.env['logistics.mail.notify'].sudo()
        awb = self.name or ''
        arn = (self.indiapost_article_number or '').strip() or _('not yet allocated')
        orig_charge = self._ip_format_money(self.indiapost_orig_charge)
        new_quote = (
            self._ip_format_money(self.indiapost_scan_quote)
            if self.indiapost_scan_quote or self.indiapost_scan_adjusted
            else _('not yet available')
        )
        if float_compare(wallet_amount, 0.0, precision_digits=2) > 0:
            wallet_line = _('Credit %s to your wallet') % self._ip_format_money(
                wallet_amount)
        elif float_compare(wallet_amount, 0.0, precision_digits=2) < 0:
            wallet_line = _('Debit %s from your wallet') % self._ip_format_money(
                abs(wallet_amount))
        else:
            wallet_line = _('No wallet movement — the quoted charge is unchanged.')
        inner = Markup(
            '<p>Hello %s,</p>'
            '<p>India Post has scanned this shipment and the billed weight, '
            'size or rate may differ from what was declared at pickup.</p>'
            '<ul>'
            '<li><strong>AWB:</strong> %s</li>'
            '<li><strong>ARN:</strong> %s</li>'
            '</ul>'
            '<p><strong>Original (at pickup)</strong></p>'
            '<ul>'
            '<li>Weight: %s</li>'
            '<li>Dimensions: %s</li>'
            '<li>Volumetric weight: %s</li>'
            '<li>Quoted charge: %s</li>'
            '</ul>'
            '<p><strong>After India Post scan</strong></p>'
            '<ul>'
            '<li>Weight: %s</li>'
            '<li>Dimensions: %s</li>'
            '<li>Volumetric weight: %s</li>'
            '<li>Quoted charge: %s</li>'
            '</ul>'
            '<p><strong>Wallet:</strong> %s</p>'
            '<p>This is a shipping-charge adjustment only. COD collected from '
            'the customer is unchanged.</p>'
        ) % (
            html_escape(seller.display_name or ''),
            html_escape(awb),
            html_escape(arn),
            html_escape(self._ip_format_weight_g(self.indiapost_orig_weight_g)),
            html_escape(self._ip_format_dims_cm(
                self.indiapost_orig_length_cm,
                self.indiapost_orig_breadth_cm,
                self.indiapost_orig_height_cm)),
            html_escape(self._ip_format_weight_g(self.indiapost_orig_volumetric_g)),
            html_escape(orig_charge),
            html_escape(self._ip_format_weight_g(self.indiapost_scan_weight_g)),
            html_escape(self._ip_format_dims_cm(
                self.indiapost_scan_length_cm,
                self.indiapost_scan_breadth_cm,
                self.indiapost_scan_height_cm)),
            html_escape(self._ip_format_weight_g(self.indiapost_scan_volumetric_g)),
            html_escape(new_quote),
            html_escape(wallet_line),
        )
        subject = _('India Post updated AWB %s') % awb
        mail = Mail._kx_queue_mail(
            email_to=email,
            subject=subject,
            body_html=Mail._kx_wrap_body(
                _('India Post scan update'), inner),
            res_model=self._name,
            res_id=self.id,
        )
        self.with_context(allow_delivery_charge_write=True).write({
            'indiapost_scan_notified': True,
        })
        return mail

    def action_indiapost_apply_scan_adjustment(self):
        """Ops: re-quote from stored/entered actuals and post the wallet line."""
        if not self.env.user.has_group('keralariders_logistics.group_logistics_admin'):
            raise UserError(_(
                'Only a Logistics Administrator can apply an India Post '
                'scan rate adjustment.'
            ))
        applied = 0
        skipped = 0
        for record in self:
            if not record._ip_scan_eligible():
                skipped += 1
                continue
            before = record.indiapost_scan_wallet_txn_id
            record._ip_apply_scan_rate_adjustment(
                extracted={'ops_quote': record.indiapost_scan_quote}
                if record.indiapost_scan_quote and not record._ip_scan_has_dims()
                else {},
                allow_tariff_http=True,
                allow_api_tariff=True,
            )
            record.invalidate_recordset([
                'indiapost_scan_adjusted', 'indiapost_scan_wallet_txn_id',
            ])
            if record.indiapost_scan_adjusted or record.indiapost_scan_seen:
                applied += 1
            elif before:
                skipped += 1
        return self._ip_notify(
            _('India Post scan adjustment'),
            _('Evaluated %(applied)s shipment(s) (%(skipped)s skipped).') % {
                'applied': applied, 'skipped': skipped,
            },
        )

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
        # Every route to "delivered" funnels through write(): the tracking sync
        # and the webhook both land in _write_with_state, so does the DE's
        # action_mark_delivered, and so does an administrator moving the
        # statusbar by hand. Hooking the write rather than any one of them is
        # what makes the credit impossible to route around.
        newly_delivered = self.browse()
        if vals.get('state') == 'delivered':
            newly_delivered = self.filtered(lambda s: s.state != 'delivered')
        res = super().write(vals)
        if newly_delivered:
            newly_delivered._ip_credit_seller_cod()
        return res

    # ------------------------------------------------------------------
    # COD on delivery
    #
    # An own-network parcel is paid in cash to the delivery executive, so its
    # COD credit is raised by the DE's own settlement (customer → DE → hub →
    # company) and the seller is cleared from the company account at the end of
    # it. India Post collects on our behalf and remits to the company directly,
    # so there is no cash custody to record: the delivery scan itself is the
    # collection, and the seller has to be credited the moment it lands.
    # ------------------------------------------------------------------
    indiapost_cod_credited = fields.Boolean(
        string='India Post COD Credited', default=False, copy=False,
        readonly=True, index=True,
        help='Set once the seller COD ledger has been credited for this '
             'delivered article. Repeated tracking polls re-write the same '
             'delivered state, so the credit is guarded by this flag and by a '
             'lookup of the payment itself.',
    )

    def _ip_cod_creditable(self):
        """The subset of these shipments whose COD is owed to the seller now."""
        return self.filtered(
            lambda s: s.fulfilment_method == 'indiapost'
            and s.state == 'delivered'
            and not s.is_return_journey
            and s.order_payment_type == 'cod'
            and s.cod_amount > 0
            and s.seller_id
            and not s.indiapost_cod_credited
        )

    def _ip_credit_seller_cod(self):
        """Raise the seller COD credit for delivered India Post COD articles.

        Idempotent twice over: the stored flag short-circuits the common case
        (a tracking poll re-writing the same delivered state), and
        ``action_create_indiapost_cod_payment`` still looks the payment up by
        shipment before creating one, so a shipment whose flag never got
        written — a backfill, a restore — cannot be credited twice either.

        A failure here must never take down a tracking batch or block a
        delivery scan, so each shipment is credited in its own savepoint.
        """
        Transfer = self.env['logistics.account.transfer'].sudo()
        credited = self.browse()
        for shipment in self._ip_cod_creditable():
            already = Transfer._indiapost_cod_payment_for_shipment(shipment)
            try:
                with self.env.cr.savepoint():
                    transfer = Transfer.action_create_indiapost_cod_payment(
                        shipment)
            except Exception:
                _logger.exception(
                    'Could not credit India Post COD for shipment %s',
                    shipment.name,
                )
                continue
            shipment.sudo().indiapost_cod_credited = True
            if already:
                # Someone had already raised the payment by hand; stamp the
                # shipment so the flag and the ledger agree, and say nothing.
                continue
            credited |= shipment
            shipment.sudo().message_post(body=_(
                'COD of %(amount)s collected by India Post credited to '
                '%(seller)s (%(ref)s).',
                amount=shipment.currency_id.format(shipment.cod_amount)
                if shipment.currency_id else shipment.cod_amount,
                seller=shipment.seller_id.display_name,
                ref=transfer.name or '',
            ))
        return credited

    @api.model
    def _ip_backfill_cod_credits(self, limit=None):
        """Credit delivered India Post COD shipments that predate this code.

        Safe to run more than once and safe to run twice in a row: it only
        picks up shipments with no COD payment behind them, and the create is
        guarded again per shipment. Run it from an odoo shell — see the
        module README notes — rather than from an upgrade hook, so crediting
        historical money stays a decision somebody makes.
        """
        domain = [
            ('fulfilment_method', '=', 'indiapost'),
            ('state', '=', 'delivered'),
            ('is_return_journey', '=', False),
            ('order_payment_type', '=', 'cod'),
            ('cod_amount', '>', 0),
            ('indiapost_cod_credited', '=', False),
        ]
        shipments = self.sudo().search(domain, limit=limit, order='id')
        # Shipments credited before the flag existed (or by hand through the
        # COD payment wizard) already have a payment; stamp them instead of
        # paying them a second time.
        Transfer = self.env['logistics.account.transfer'].sudo()
        already_paid = shipments.filtered(
            lambda s: Transfer._indiapost_cod_payment_for_shipment(s)
        )
        if already_paid:
            already_paid.write({'indiapost_cod_credited': True})
        credited = (shipments - already_paid)._ip_credit_seller_cod()
        _logger.info(
            'India Post COD backfill: %s credited, %s already had a payment, '
            '%s considered.',
            len(credited), len(already_paid), len(shipments),
        )
        return credited

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
        res = super().action_add_wallet_transaction()
        self._ip_snapshot_charged_package()
        return res

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
            article_type=self._ip_product(),
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
        """Where India Post collects: the shipment pickup / seller pin."""
        self.ensure_one()
        seller = self.seller_id
        candidates = [
            self.shipping_from_zip,
            seller.zip if seller else None,
            seller.partner_id.zip if seller and seller.partner_id else None,
        ]
        for candidate in candidates:
            if (candidate or '').strip():
                try:
                    return ipc.normalize_pincode(
                        candidate, _('Seller pickup pincode'))
                except ipc.IndiapostDataError as exc:
                    raise UserError(str(exc)) from exc
        raise UserError(_(
            'The seller has no pickup pincode, so India Post cannot collect '
            'this shipment. Add a 6-digit pincode to the seller pickup address.'
        ))

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

    def _ip_sender_group(self, settings, parts=None):
        """Physical from-address: the seller, which India Post prints as SENDER.

        KeralaXpress stays the bulk customer / contract holder
        (``bulk_customer_id``, ``contract_id``). These ``sender_*`` fields are
        the pickup identity on both booking and the official CEPT label, so
        they must not be the company warehouse when a seller address exists.
        GSTIN / email remain the contract holder's, when configured.
        """
        parts = parts or self._ip_seller_address_parts()
        payload = {
            'sender_name': parts['name'],
            'sender_company': parts['company'],
            'sender_add_line_1': parts['address'][0],
            'sender_city': parts['city'],
            'sender_state': parts['state'],
            'sender_pincode': parts['pincode'],
            'sender_mobile_no': parts['mobile'],
        }
        if parts['address'][1]:
            payload['sender_add_line_2'] = parts['address'][1]
        if parts['address'][2]:
            payload['sender_add_line_3'] = parts['address'][2]
        if settings.get('indiapost_sender_email'):
            payload['sender_emailid'] = ipc.normalize_text(
                settings['indiapost_sender_email'], _('Consignor email'))
        if settings.get('indiapost_sender_gstin'):
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

    def _ip_seller_mobile(self):
        """10-digit India mobile India Post will call for pickup."""
        self.ensure_one()
        seller = self.seller_id
        partner = seller.partner_id if seller else self.env['res.partner']
        candidates = []
        if seller:
            candidates.append(seller.phone)
        if partner:
            candidates.append(partner.phone)
            # Odoo 19 ``res.partner`` has no ``mobile``; keep a defensive
            # lookup so a custom field still wins over a blank phone.
            if 'mobile' in partner._fields:
                candidates.append(partner.mobile)
        for candidate in candidates:
            if not (candidate or '').strip():
                continue
            try:
                return ipc.normalize_mobile(
                    candidate, _('Seller mobile number'))
            except ipc.IndiapostDataError as exc:
                raise UserError(str(exc)) from exc
        raise UserError(_(
            'Seller mobile number is missing for %(awb)s, so India Post '
            'cannot schedule pickup. Add a 10-digit Indian mobile on the '
            'seller profile.'
        ) % {'awb': self.name or _('this shipment')})

    def _ip_seller_address_parts(self):
        """The seller pickup address used for sender, pickup, and returns.

        Source of truth is the shipment's shipping-from / seller pickup
        address already shown on the portal. Never the company warehouse.
        """
        self.ensure_one()
        seller = self.seller_id
        if not seller:
            raise UserError(_(
                'This shipment has no seller, so India Post has nowhere to '
                'collect from.'
            ))
        pincode = self._ip_origin_pincode()
        raw_address = (self.shipping_from_address or '').strip() or '\n'.join(
            part for part in (seller.street, seller.street2) if part)
        try:
            address = ipc.split_address_lines(
                raw_address, _('Seller pickup address'))
            city, state = self._ip_locality(
                pincode,
                fallback_city=(
                    seller.city
                    or (self.shipping_from_district_id.name if self.shipping_from_district_id else '')
                    or (seller.district_id.name if seller.district_id else '')
                ),
                fallback_state=(
                    (self.shipping_from_state_id.name if self.shipping_from_state_id else '')
                    or (seller.state_id.name if seller.state_id else '')
                ),
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
                'mobile': self._ip_seller_mobile(),
            }
        except ipc.IndiapostDataError as exc:
            raise UserError(str(exc)) from exc

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
            'contract_id': self._ip_contract_id(settings),
            'barcode_no': barcode,
            'pickup_or_dropoff': 'PICKUP',
            'pickup_dropoff_office_id': office.office_id,
            'article_type': self._ip_product(),
            # Must be a whole number of grams: 1500.5 is rejected with
            # "Physical weight must be a whole number".
            'physical_weight': grams,
            # Booking only accepts article_type SP/BP. Parcel vs document is
            # this shape: NROL (or ROLL) so a light Speed Post article is not
            # booked as a document after being quoted as SP_INLAND_PARCEL.
            'shape_of_article': ipc.resolve_shape(
                grams, cylindrical=self.is_cylindrical),
            'length': ipc.cm_to_int(self.length_cm),
            'breadth_diameter': ipc.cm_to_int(self.breadth_cm),
            'height': ipc.cm_to_int(self.height_cm),
            # Our own AWB, so a booking can always be traced back even if the
            # response echoes identifiers we did not expect.
            'bulk_reference': (self.name or '')[:ipc.BULK_REFERENCE_MAX_LEN],
        }
        article.update(self._ip_sender_group(settings, parts=seller_parts))
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
            and not s.indiapost_booking_in_progress
            and (not s.indiapost_barcode_id
                 or s.indiapost_barcode_id.state != 'booked')
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
        another one. Concurrent clicks wait on a row lock, then see the
        stored article / in-flight flag and return without posting again.
        """
        if self.ids:
            self.env.cr.execute(
                'SELECT id FROM logistics_shipment WHERE id IN %s FOR UPDATE',
                [tuple(self.ids)],
            )
            self.invalidate_recordset([
                'indiapost_article_number', 'indiapost_booking_state',
                'indiapost_booking_in_progress', 'indiapost_barcode_id',
            ])

        settings = self.env['logistics.indiapost.client']._ip_require_configured()
        self._ip_check_booking_settings(settings)

        candidates = self._ip_bookable()
        already = self.filtered(lambda s: (
            s.indiapost_article_number
            or s.indiapost_booking_state == 'booked'
            or (s.indiapost_barcode_id and s.indiapost_barcode_id.state == 'booked')
        ))
        in_flight = self.filtered('indiapost_booking_in_progress')
        skipped = self - candidates - already - in_flight
        if not candidates:
            if in_flight and not already:
                return self._ip_notify(
                    _('India Post booking already in progress'),
                    _('Wait for the current booking to finish before sending again.'),
                    kind='warning',
                )
            return self._ip_notify(
                _('Nothing to book'),
                _('%(booked)s shipment(s) are already booked and %(skipped)s '
                  'are not eligible for India Post.')
                % {'booked': len(already), 'skipped': len(skipped)},
                kind='warning',
            )

        candidates.sudo().write({'indiapost_booking_in_progress': True})
        self.env.flush_all()
        booked_count = 0
        failed = []
        try:
            for chunk_start in range(0, len(candidates), BOOKING_CHUNK_SIZE):
                chunk = candidates[chunk_start:chunk_start + BOOKING_CHUNK_SIZE]
                booked, errors = chunk._ip_book_chunk(settings)
                booked_count += booked
                failed.extend(errors)
        finally:
            still = candidates.exists().filtered('indiapost_booking_in_progress')
            if still:
                still.sudo().write({'indiapost_booking_in_progress': False})

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
        if not ipc.BULK_CUSTOMER_ID_RE.match(customer_id):
            raise UserError(_(
                'The India Post bulk customer id must be exactly 10 digits. '
                'Set it under Settings > Logistics > India Post.'
            ))
        # Only the contracts this batch actually needs, so a Speed Post run is
        # not held up by a Business Parcel contract that is still blank.
        Client = self.env['logistics.indiapost.client']
        for article_type in sorted({shipment._ip_product()
                                    for shipment in self._ip_bookable()}):
            Client._ip_contract_id(settings, article_type)

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
                shipment=self, article_type=self._ip_product())
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
            'indiapost_booking_in_progress': False,
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
            'indiapost_booking_in_progress': False,
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
            'service_type': self._ip_product(),
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
            if shipment.indiapost_label_pdf:
                continue
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
                'indiapost_sort_code': shipment._ip_sort_code_from_label(
                    response.content, payload.get('transmission_mode')),
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

    def _ip_label_pdf_bytes(self):
        """Raw bytes of the stored India Post label, or empty."""
        self.ensure_one()
        data = self.indiapost_label_pdf
        if not data:
            return b''
        if isinstance(data, bytes) and data[:4] == b'%PDF':
            return data
        try:
            decoded = base64.b64decode(data)
        except (TypeError, ValueError):
            return b''
        return decoded if decoded[:4] == b'%PDF' else b''

    @staticmethod
    def _ip_sort_code_from_label(pdf_bytes, transmission_mode=None):
        """Letter for the AWB PIN box: PDF first, then the mode we sent."""
        parsed = ipc.parse_label_sort_code_from_pdf(pdf_bytes or b'')
        if parsed:
            return parsed
        mode = (transmission_mode or '').strip().upper()
        if ipc.LABEL_SORT_TOKEN_RE.fullmatch(mode):
            return mode
        return False

    def _awb_indiapost_sort_code(self):
        """Letter printed in the destination PIN box, or '' if unknown.

        Prefers the value persisted at label fetch. Existing shipments that
        already store a CEPT PDF are parsed on the fly. Never invents PIN or S.
        """
        self.ensure_one()
        stored = (self.indiapost_sort_code or '').strip().upper()
        if stored:
            return stored
        # Portal sellers cannot read indiapost_label_pdf (staff-only binary).
        # Print AWB still needs the sort letter, so parse via sudo without
        # exposing the PDF to the portal user.
        parsed = ipc.parse_label_sort_code_from_pdf(
            self.sudo()._ip_label_pdf_bytes())
        return parsed or ''

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
                'indiapost_booking_in_progress': False,
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
