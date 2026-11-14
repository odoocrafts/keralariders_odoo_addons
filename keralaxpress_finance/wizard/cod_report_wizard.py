from odoo import fields, models


class CodReportWizard(models.TransientModel):
    _name = 'kx.cod.report.wizard'
    _inherit = ['kx.download.mixin']
    _description = 'COD Wallet Report'

    date_from = fields.Date(string='From')
    date_to = fields.Date(string='To', default=fields.Date.context_today)
    seller_ids = fields.Many2many('logistics.seller', string='Sellers',
                                  help='Leave empty for all sellers.')

    def action_export_xlsx(self):
        self.ensure_one()
        content = self.env['kx.report.service'].cod_report_xlsx(
            self.date_from, self.date_to, self.seller_ids.ids or None)
        return self._kx_download(content, 'COD_Wallet_Report_%s.xlsx' % (
            self.date_to or 'today'))
