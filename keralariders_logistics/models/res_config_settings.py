from odoo import models, fields, api, _
from odoo.exceptions import ValidationError

from . import indiapost_common as ipc
from .indiapost_client import CONFIG_PREFIX, DEFAULT_BASE_URL


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    logistics_upi_id = fields.Char(
        string="Logistics UPI ID",
        config_parameter='keralariders_logistics.logistics_upi_id',
    )
    company_cod_account_id = fields.Many2one(
        'logistics.account',
        string="Company COD Settlement Account",
        domain="[('account_type', 'in', ('company', 'bank', 'cash'))]",
        help="Company account that receives hub banking and pays seller COD clearances.",
    )

    # -------------------------------------------------------------------------
    # India Post (Department of Posts) bulk customer API
    # -------------------------------------------------------------------------
    indiapost_enabled = fields.Boolean(
        string="Enable India Post",
        config_parameter=CONFIG_PREFIX + 'indiapost_enabled',
        help="Master switch. When off, no India Post calls are made at all and "
             "the rate calculator falls back to the weight slab table.",
    )
    indiapost_environment = fields.Selection(
        [('sandbox', 'Sandbox / UAT'), ('production', 'Production')],
        string="Environment", default='sandbox',
        config_parameter=CONFIG_PREFIX + 'indiapost_environment',
        help="Also selects which barcode ranges may be allocated.",
    )
    indiapost_base_url = fields.Char(
        string="Base URL", default=DEFAULT_BASE_URL,
        config_parameter=CONFIG_PREFIX + 'indiapost_base_url',
    )
    indiapost_username = fields.Char(
        string="Username", config_parameter=CONFIG_PREFIX + 'indiapost_username',
        help="Same value as the bulk customer id.",
    )
    indiapost_password = fields.Char(
        string="Password", config_parameter=CONFIG_PREFIX + 'indiapost_password',
    )
    indiapost_customer_id = fields.Char(
        string="Bulk Customer Id",
        config_parameter=CONFIG_PREFIX + 'indiapost_customer_id',
        help="Exactly 10 digits.",
    )
    indiapost_contract_id = fields.Char(
        string="Contract Id",
        config_parameter=CONFIG_PREFIX + 'indiapost_contract_id',
        help="Exactly 8 digits, and it must be a Speed Post contract. Bookings "
             "are rejected outright if the contract has no service type "
             "defined, which is the case for a brand new customer id until "
             "India Post activates the contract.",
    )

    # Consignor of record. KeralaXpress books as a single consignor; the seller
    # appears as the pickup and return address instead.
    indiapost_sender_name = fields.Char(
        string="Consignor Name",
        config_parameter=CONFIG_PREFIX + 'indiapost_sender_name',
    )
    indiapost_sender_company = fields.Char(
        string="Consignor Company",
        config_parameter=CONFIG_PREFIX + 'indiapost_sender_company',
    )
    indiapost_sender_address = fields.Text(
        string="Consignor Address",
        config_parameter=CONFIG_PREFIX + 'indiapost_sender_address',
        help="Split across up to three API address lines of 80 characters each.",
    )
    indiapost_sender_city = fields.Char(
        string="Consignor City",
        config_parameter=CONFIG_PREFIX + 'indiapost_sender_city',
    )
    indiapost_sender_state = fields.Char(
        string="Consignor State",
        config_parameter=CONFIG_PREFIX + 'indiapost_sender_state',
    )
    indiapost_sender_pincode = fields.Char(
        string="Consignor Pincode",
        config_parameter=CONFIG_PREFIX + 'indiapost_sender_pincode',
    )
    indiapost_sender_mobile = fields.Char(
        string="Consignor Mobile",
        config_parameter=CONFIG_PREFIX + 'indiapost_sender_mobile',
    )
    indiapost_sender_email = fields.Char(
        string="Consignor Email",
        config_parameter=CONFIG_PREFIX + 'indiapost_sender_email',
    )
    indiapost_sender_gstin = fields.Char(
        string="Consignor GSTIN",
        config_parameter=CONFIG_PREFIX + 'indiapost_sender_gstin',
    )

    indiapost_booking_office_name = fields.Char(
        string="Booking Office Name",
        config_parameter=CONFIG_PREFIX + 'indiapost_booking_office_name',
        help="Printed on the address label. Defaults to the consignor city.",
    )
    indiapost_booking_office_pin = fields.Char(
        string="Booking Office Pincode",
        config_parameter=CONFIG_PREFIX + 'indiapost_booking_office_pin',
    )
    indiapost_label_size = fields.Selection(
        [('A6', 'A6'), ('A7', 'A7')], string="Label Size", default='A6',
        config_parameter=CONFIG_PREFIX + 'indiapost_label_size',
    )
    indiapost_transmission_mode = fields.Selection(
        [('S', 'Surface'), ('A', 'Air')], string="Transmission Mode", default='S',
        config_parameter=CONFIG_PREFIX + 'indiapost_transmission_mode',
    )

    indiapost_tariff_cache_minutes = fields.Integer(
        string="Rate Cache (minutes)", default=30,
        config_parameter=CONFIG_PREFIX + 'indiapost_tariff_cache_minutes',
        help="How long a quote is reused. Keeps the public rate calculator "
             "from hammering the India Post tariff endpoint.",
    )
    indiapost_office_cache_days = fields.Integer(
        string="Office Cache (days)", default=30,
        config_parameter=CONFIG_PREFIX + 'indiapost_office_cache_days',
    )
    indiapost_request_timeout = fields.Integer(
        string="Read Timeout (seconds)", default=60,
        config_parameter=CONFIG_PREFIX + 'indiapost_request_timeout',
    )
    indiapost_pickup_lead_days = fields.Integer(
        string="Pickup Lead Time (days)", default=1,
        config_parameter=CONFIG_PREFIX + 'indiapost_pickup_lead_days',
        help="Earliest pickup date offered to sellers, counted from today.",
    )
    indiapost_default_pod = fields.Boolean(
        string="Request Proof of Delivery by Default",
        config_parameter=CONFIG_PREFIX + 'indiapost_default_pod',
    )
    indiapost_quote_markup_percent = fields.Float(
        string="Quote Markup (%)", default=0.0,
        config_parameter=CONFIG_PREFIX + 'indiapost_quote_markup_percent',
        help="Added on top of the India Post total when quoting sellers. Zero "
             "means pure passthrough.",
    )

    @api.model
    def get_values(self):
        res = super().get_values()
        account_id = self.env['ir.config_parameter'].sudo().get_param(
            'keralariders_logistics.company_cod_account_id'
        )
        res['company_cod_account_id'] = int(account_id) if account_id else False
        return res

    def set_values(self):
        self._check_indiapost_settings()
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param(
            'keralariders_logistics.company_cod_account_id',
            self.company_cod_account_id.id if self.company_cod_account_id else '',
        )
        # Credentials may have changed, so drop any cached bearer token.
        self.env['logistics.indiapost.client']._ip_forget_token()

    def _check_indiapost_settings(self):
        """Validate the consignor identity before it can break a booking.

        India Post enforces almost none of its own documented rules: a 2-letter
        city and a 9-digit mobile were both accepted for booking. Catching them
        here means a misconfiguration surfaces on the settings screen instead of
        as a parcel nobody can deliver.
        """
        self.ensure_one()
        if not self.indiapost_enabled:
            return
        problems = []

        def check(callback, *args):
            try:
                callback(*args)
            except ipc.IndiapostDataError as exc:
                problems.append(str(exc))

        customer_id = (self.indiapost_customer_id or '').strip()
        if customer_id and not ipc.BULK_CUSTOMER_ID_RE.match(customer_id):
            problems.append(_('The bulk customer id must be exactly 10 digits.'))
        contract_id = (self.indiapost_contract_id or '').strip()
        if contract_id and not ipc.CONTRACT_ID_RE.match(contract_id):
            problems.append(_('The contract id must be exactly 8 digits.'))

        check(ipc.normalize_text, self.indiapost_sender_name, _('Consignor name'))
        check(ipc.normalize_text, self.indiapost_sender_company,
              _('Consignor company'))
        check(ipc.split_address_lines, self.indiapost_sender_address,
              _('Consignor address'))
        check(ipc.normalize_text, self.indiapost_sender_city, _('Consignor city'))
        check(ipc.normalize_text, self.indiapost_sender_state, _('Consignor state'))
        check(ipc.normalize_pincode, self.indiapost_sender_pincode,
              _('Consignor pincode'))
        check(ipc.normalize_mobile, self.indiapost_sender_mobile,
              _('Consignor mobile'))
        if self.indiapost_sender_email:
            check(ipc.normalize_text, self.indiapost_sender_email,
                  _('Consignor email'))
        if self.indiapost_booking_office_pin:
            check(ipc.normalize_pincode, self.indiapost_booking_office_pin,
                  _('Booking office pincode'))

        if problems:
            raise ValidationError(
                _('The India Post configuration is incomplete:\n\n%s')
                % '\n'.join('• %s' % problem for problem in problems)
            )

    def action_indiapost_test_connection(self):
        self.ensure_one()
        # Persist first so the test uses what is on screen.
        self.set_values()
        return self.env['logistics.indiapost.client'].action_ip_test_connection()
