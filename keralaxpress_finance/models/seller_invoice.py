import calendar
from datetime import date

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError
from odoo.tools.misc import format_date

ADMIN_GROUP = 'keralariders_logistics.group_logistics_admin'

# Ledger categories and where they land on the invoice. The sign is the
# invoice's: a charge adds to what the seller is billed, a refund takes off.
CHARGE_LINES = (
    # (line key, ledger category, side of the ledger, label)
    ('shipment', 'booking', 'debit', 'Shipment charges'),
    ('scan_adjustment', 'scan_adjustment', 'debit', 'India Post rate adjustments'),
    ('cod_delivery_restore', 'cod_delivery_restore', 'debit',
     'Delivery charges restored on COD returns'),
    ('cod_charge', 'cod_charge', 'debit', 'COD settlement charges'),
    ('manual', 'manual', 'debit', 'Other charges'),
)
REFUND_LINES = (
    ('cancel_refund', 'cancel_refund', 'credit', 'Cancellation refunds'),
    ('cod_delivery_refund', 'cod_delivery_refund', 'credit',
     'Delivery charges refunded on COD pickup'),
    ('scan_adjustment_credit', 'scan_adjustment', 'credit',
     'India Post rate adjustment credits'),
)
LINE_KEYS = [(key, label) for key, _cat, _side, label in CHARGE_LINES + REFUND_LINES]

# Fields that make up what was billed. Frozen once an invoice is posted.
FROZEN_FIELDS = {
    'seller_id', 'date_from', 'date_to', 'invoice_date', 'line_ids',
    'recharge_total', 'booking_count', 'shipment_charges', 'additional_charges',
    'refunds', 'amount_total', 'gst_percent', 'taxable_amount', 'tax_amount',
    'cgst_amount', 'sgst_amount', 'igst_amount', 'tax_mode', 'sac_code',
    'seller_gstin', 'seller_state_id', 'company_gstin',
}


class SellerInvoice(models.Model):
    _name = 'kx.seller.invoice'
    _description = 'Seller Monthly Invoice'
    _inherit = ['mail.thread']
    _order = 'date_from desc, id desc'

    name = fields.Char(string='Invoice Number', readonly=True, copy=False, default='/')
    seller_id = fields.Many2one(
        'logistics.seller', string='Seller', required=True, readonly=True,
        index=True, ondelete='restrict')
    partner_id = fields.Many2one(related='seller_id.partner_id', string='Partner')
    date_from = fields.Date(string='Period Start', required=True, readonly=True, index=True)
    date_to = fields.Date(string='Period End', required=True, readonly=True)
    period_label = fields.Char(string='Period', compute='_compute_period_label', store=True)
    invoice_date = fields.Date(string='Invoice Date', readonly=True)
    state = fields.Selection(
        [('draft', 'Draft'), ('posted', 'Posted'), ('cancelled', 'Cancelled')],
        string='Status', default='draft', required=True, readonly=True,
        tracking=True, index=True)
    company_id = fields.Many2one(
        'res.company', string='Company', required=True, readonly=True,
        default=lambda self: self.env.company)
    currency_id = fields.Many2one(
        'res.currency', string='Currency', readonly=True,
        default=lambda self: self.env.company.currency_id)
    line_ids = fields.One2many('kx.seller.invoice.line', 'invoice_id', string='Lines', readonly=True)

    recharge_total = fields.Monetary(string='Total Recharge', readonly=True)
    booking_count = fields.Integer(string='Bookings', readonly=True)
    shipment_charges = fields.Monetary(string='Shipment Charges', readonly=True)
    additional_charges = fields.Monetary(string='Additional Charges', readonly=True)
    refunds = fields.Monetary(string='Refunds & Credits', readonly=True)
    amount_total = fields.Monetary(string='Invoice Total (incl. GST)', readonly=True)
    gst_percent = fields.Float(string='GST (%)', digits=(5, 2), readonly=True)
    taxable_amount = fields.Monetary(string='Taxable Value', readonly=True)
    tax_amount = fields.Monetary(string='GST', readonly=True)
    tax_mode = fields.Selection(
        [('intra', 'CGST + SGST'), ('inter', 'IGST')], string='Tax Type', readonly=True)
    cgst_amount = fields.Monetary(string='CGST', readonly=True)
    sgst_amount = fields.Monetary(string='SGST', readonly=True)
    igst_amount = fields.Monetary(string='IGST', readonly=True)
    sac_code = fields.Char(string='SAC', readonly=True)
    seller_gstin = fields.Char(string='Seller GSTIN', readonly=True)
    seller_state_id = fields.Many2one('res.country.state', string='Place of Supply', readonly=True)
    company_gstin = fields.Char(string='Company GSTIN', readonly=True)

    _seller_period_uniq = models.UniqueIndex(
        "(seller_id, date_from) WHERE state != 'cancelled'",
        "A seller can only have one active invoice per month. Cancel the "
        "existing invoice before generating it again.",
    )

    @api.depends('date_from')
    def _compute_period_label(self):
        for inv in self:
            inv.period_label = (
                format_date(self.env, inv.date_from, date_format='MMMM y')
                if inv.date_from else '')

    # ------------------------------------------------------------------
    # Access and integrity
    # ------------------------------------------------------------------
    def _check_admin(self):
        if not self.env.su and not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError(_(
                "Only a Logistics Administrator can issue, cancel or reset "
                "seller invoices."))

    def write(self, vals):
        if not self.env.context.get('kx_invoice_write'):
            frozen = FROZEN_FIELDS & set(vals)
            if frozen and self.filtered(lambda inv: inv.state != 'draft'):
                raise UserError(_(
                    "A posted or cancelled invoice cannot be changed (%s). "
                    "Cancel it and generate the month again instead.",
                    ', '.join(sorted(frozen))))
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _unlink_only_draft(self):
        if self.filtered(lambda inv: inv.state != 'draft'):
            raise UserError(_("Only draft invoices can be deleted. Cancel it instead."))

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------
    @api.model
    def _month_bounds(self, year, month):
        start = date(year, month, 1)
        return start, date(year, month, calendar.monthrange(year, month)[1])

    @api.model
    def _period_values(self, seller, date_from, date_to):
        """Everything an invoice for ``seller`` over the period would carry."""
        currency = self.env.company.currency_id
        Line = self.env['kx.seller.ledger.line'].sudo()
        lines = Line.search([
            ('seller_id', '=', seller.id),
            ('date', '>=', date_from),
            ('date', '<=', date_to),
        ])

        def pick(category, side):
            return lines.filtered(
                lambda l: l.category == category and (l[side] or 0.0) > 0.0)

        invoice_lines = []
        totals = {'charge': 0.0, 'refund': 0.0}
        shipment_charges = additional = 0.0
        booking_count = 0
        for kind, spec in (('charge', CHARGE_LINES), ('refund', REFUND_LINES)):
            for sequence, (key, category, side, label) in enumerate(spec, start=1):
                matched = pick(category, side)
                amount = currency.round(sum(matched.mapped(side)))
                if not matched:
                    continue
                count = len(matched.shipment_id) or len(matched)
                if key == 'shipment':
                    shipment_charges = amount
                    booking_count = len(matched.shipment_id)
                elif kind == 'charge':
                    additional += amount
                totals[kind] += amount
                invoice_lines.append({
                    'sequence': sequence + (0 if kind == 'charge' else 100),
                    'line_key': key,
                    'name': label,
                    'quantity': count,
                    'amount': amount if kind == 'charge' else -amount,
                })
        recharge_total = currency.round(sum(
            lines.filtered(lambda l: l.category == 'recharge').mapped('credit')))
        amount_total = currency.round(max(totals['charge'] - totals['refund'], 0.0))

        gst_percent, sac_code = self.env['res.config.settings'].kx_invoice_tax_settings()
        taxable = currency.round(amount_total * 100.0 / (100.0 + gst_percent))
        tax = currency.round(amount_total - taxable)
        company = self.env.company
        seller_state = seller.state_id
        inter = bool(seller_state and company.state_id and seller_state != company.state_id)
        cgst = sgst = igst = 0.0
        if inter:
            igst = tax
        else:
            cgst = currency.round(tax / 2.0)
            sgst = currency.round(tax - cgst)
        return {
            'recharge_total': recharge_total,
            'booking_count': booking_count,
            'shipment_charges': shipment_charges,
            'additional_charges': currency.round(additional),
            'refunds': currency.round(totals['refund']),
            'amount_total': amount_total,
            'gst_percent': gst_percent,
            'taxable_amount': taxable,
            'tax_amount': tax,
            'tax_mode': 'inter' if inter else 'intra',
            'cgst_amount': cgst,
            'sgst_amount': sgst,
            'igst_amount': igst,
            'sac_code': sac_code,
            'seller_gstin': seller.tax_id or False,
            'seller_state_id': seller_state.id or False,
            'company_gstin': company.vat or False,
            'line_ids': [(5, 0, 0)] + [(0, 0, vals) for vals in invoice_lines],
        }

    @api.model
    def _has_billable_activity(self, values):
        return bool(values['amount_total'] or values['recharge_total']
                    or values['refunds'] or values['shipment_charges'])

    @api.model
    def generate_for_period(self, date_from, date_to, sellers=None,
                            regenerate=False, post=True):
        """Create the invoices for one period; return the ones created.

        A seller that already has an active invoice for the period is skipped
        unless ``regenerate`` is set, in which case that invoice is cancelled
        and a fresh snapshot is taken. Sellers with nothing to bill or show
        are skipped.
        """
        self._check_admin()
        date_from = fields.Date.to_date(date_from)
        date_to = fields.Date.to_date(date_to)
        if date_from > date_to:
            raise UserError(_("The period start must be before its end."))
        if sellers is None:
            groups = self.env['kx.seller.ledger.line'].sudo()._read_group(
                [('date', '>=', date_from), ('date', '<=', date_to)],
                ['seller_id'], [])
            sellers = self.env['logistics.seller'].browse(
                [seller.id for (seller,) in groups if seller])
        created = self.browse()
        for seller in sellers:
            existing = self.search([
                ('seller_id', '=', seller.id),
                ('date_from', '=', date_from),
                ('state', '!=', 'cancelled'),
            ])
            if existing and not regenerate:
                continue
            values = self._period_values(seller, date_from, date_to)
            if not self._has_billable_activity(values):
                continue
            if existing:
                existing.with_context(kx_invoice_write=True).write({'state': 'cancelled'})
                # The unique index is checked on INSERT; the cancel must be in
                # the database before the replacement is created.
                existing.flush_recordset(['state'])
                for inv in existing:
                    inv.message_post(body=_("Cancelled: the month was generated again."))
            values.update({
                'seller_id': seller.id,
                'date_from': date_from,
                'date_to': date_to,
            })
            invoice = self.create(values)
            if post:
                invoice.action_post()
            created |= invoice
        return created

    @api.model
    def _cron_generate_previous_month(self):
        today = fields.Date.context_today(self)
        previous = today - relativedelta(months=1)
        date_from, date_to = self._month_bounds(previous.year, previous.month)
        return self.sudo().generate_for_period(date_from, date_to, post=True)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_recompute(self):
        self._check_admin()
        for inv in self:
            if inv.state != 'draft':
                raise UserError(_("Only draft invoices can be recomputed."))
            inv.write(inv._period_values(inv.seller_id, inv.date_from, inv.date_to))
        return True

    def action_post(self):
        self._check_admin()
        for inv in self:
            if inv.state != 'draft':
                raise UserError(_("Only draft invoices can be posted."))
            vals = {'state': 'posted'}
            if not inv.invoice_date:
                vals['invoice_date'] = fields.Date.context_today(inv)
            if not inv.name or inv.name == '/':
                # ir_sequence_date makes %(year)s/%(month)s the billed month,
                # not the day the invoice happens to be posted.
                vals['name'] = self.env['ir.sequence'].sudo().with_context(
                    ir_sequence_date=fields.Date.to_string(inv.date_to),
                ).next_by_code('kx.seller.invoice') or '/'
            inv.with_context(kx_invoice_write=True).write(vals)
        return True

    def action_cancel(self):
        self._check_admin()
        self.with_context(kx_invoice_write=True).write({'state': 'cancelled'})
        return True

    def action_print(self):
        return self.env.ref(
            'keralaxpress_finance.action_report_seller_invoice').report_action(self)

    def _report_filename(self):
        self.ensure_one()
        return 'Invoice-%s' % (self.name or self.period_label or self.id)

    def _amount_in_words(self):
        self.ensure_one()
        return self.currency_id.amount_to_text(self.amount_total)


class SellerInvoiceLine(models.Model):
    _name = 'kx.seller.invoice.line'
    _description = 'Seller Invoice Line'
    _order = 'sequence, id'

    invoice_id = fields.Many2one(
        'kx.seller.invoice', string='Invoice', required=True, ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    line_key = fields.Selection(LINE_KEYS, string='Type', readonly=True)
    name = fields.Char(string='Description', required=True)
    quantity = fields.Integer(string='Qty')
    amount = fields.Monetary(string='Amount', currency_field='currency_id')
    currency_id = fields.Many2one(related='invoice_id.currency_id')
