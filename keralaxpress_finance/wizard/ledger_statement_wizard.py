from odoo import fields, models, _
from odoo.exceptions import UserError


class LedgerStatementWizard(models.TransientModel):
    _name = 'kx.ledger.statement.wizard'
    _inherit = ['kx.download.mixin']
    _description = 'Seller Ledger Statement'

    seller_ids = fields.Many2many('logistics.seller', string='Sellers',
                                  help='Leave empty for every seller with ledger activity.')
    date_from = fields.Date(
        string='From', default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(string='To', default=fields.Date.context_today)
    source = fields.Selection(
        [('wallet', 'Wallet only'), ('cod', 'COD only')], string='Lines',
        help='Leave empty to show wallet and COD lines together.')

    def _sellers(self):
        if self.seller_ids:
            return self.seller_ids
        groups = self.env['kx.seller.ledger.line']._read_group([], ['seller_id'], [])
        sellers = self.env['logistics.seller'].browse([s.id for (s,) in groups if s])
        if not sellers:
            raise UserError(_("No seller has any ledger activity yet."))
        return sellers.sorted(lambda s: (s.name or '').lower())

    def _report_data(self):
        return {
            'date_from': fields.Date.to_string(self.date_from) if self.date_from else False,
            'date_to': fields.Date.to_string(self.date_to) if self.date_to else False,
            'source': self.source or False,
        }

    def action_export_xlsx(self):
        self.ensure_one()
        content = self.env['kx.report.service'].ledger_statement_xlsx(
            self._sellers(), self.date_from, self.date_to, self.source)
        return self._kx_download(content, 'Seller_Ledger_%s_%s.xlsx' % (
            self.date_from or 'start', self.date_to or 'today'))

    def action_print_pdf(self):
        self.ensure_one()
        return self.env.ref(
            'keralaxpress_finance.action_report_ledger_statement'
        ).report_action(self._sellers(), data=self._report_data())

    def action_view_lines(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'keralaxpress_finance.action_kx_seller_ledger_line')
        domain = []
        if self.seller_ids:
            domain.append(('seller_id', 'in', self.seller_ids.ids))
        if self.date_from:
            domain.append(('date', '>=', self.date_from))
        if self.date_to:
            domain.append(('date', '<=', self.date_to))
        if self.source:
            domain.append(('source', '=', self.source))
        action['domain'] = domain
        return action
