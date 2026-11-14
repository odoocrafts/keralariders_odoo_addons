from dateutil.relativedelta import relativedelta

from odoo import api, fields, models, _

MONTHS = [(str(m), name) for m, name in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July',
     'August', 'September', 'October', 'November', 'December'], start=1)]


class InvoiceGenerateWizard(models.TransientModel):
    _name = 'kx.invoice.generate.wizard'
    _description = 'Generate Seller Invoices'

    @api.model
    def _default_previous(self):
        return fields.Date.context_today(self) - relativedelta(months=1)

    month = fields.Selection(
        MONTHS, string='Month', required=True,
        default=lambda self: str(self._default_previous().month))
    year = fields.Integer(
        string='Year', required=True, default=lambda self: self._default_previous().year)
    seller_ids = fields.Many2many(
        'logistics.seller', string='Sellers',
        help='Leave empty for every seller with activity in the month.')
    regenerate = fields.Boolean(
        string='Replace existing invoices',
        help='Cancel the invoice a seller already has for this month and issue a '
             'new one from the current ledger.')
    post = fields.Boolean(string='Post immediately', default=True,
                          help='Untick to review the invoices as drafts first.')

    def action_generate(self):
        self.ensure_one()
        Invoice = self.env['kx.seller.invoice']
        date_from, date_to = Invoice._month_bounds(self.year, int(self.month))
        invoices = Invoice.generate_for_period(
            date_from, date_to,
            sellers=self.seller_ids or None,
            regenerate=self.regenerate,
            post=self.post,
        )
        action = self.env['ir.actions.act_window']._for_xml_id(
            'keralaxpress_finance.action_kx_seller_invoice')
        action['domain'] = [('id', 'in', invoices.ids)]
        action['name'] = _('Invoices generated for %(month)s %(year)s',
                           month=dict(MONTHS)[self.month], year=self.year)
        action['context'] = {}
        return action
