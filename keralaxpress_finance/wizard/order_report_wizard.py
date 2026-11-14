from odoo import api, fields, models

from odoo.addons.keralariders_logistics.models.shipment import delivery_states


class OrderReportWizard(models.TransientModel):
    _name = 'kx.order.report.wizard'
    _inherit = ['kx.download.mixin']
    _description = 'Order Report'

    date_from = fields.Date(
        string='From', default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(string='To', default=fields.Date.context_today)
    seller_ids = fields.Many2many('logistics.seller', string='Sellers',
                                  help='Leave empty for all sellers.')
    state_filter = fields.Selection(
        delivery_states, string='Status', help='Leave empty for every status.')

    @api.model
    def _service(self):
        return self.env['kx.report.service']

    def _states(self):
        return [self.state_filter] if self.state_filter else None

    def action_export_xlsx(self):
        self.ensure_one()
        content = self._service().order_report_xlsx(
            self.date_from, self.date_to, self.seller_ids.ids or None, self._states())
        return self._kx_download(content, 'Order_Report_%s_%s.xlsx' % (
            self.date_from or 'start', self.date_to or 'today'))

    def action_open_analysis(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id(
            'keralaxpress_finance.action_kx_order_analysis')
        action['domain'] = self._service()._order_domain(
            self.date_from, self.date_to, self.seller_ids.ids or None, self._states())
        return action
