from odoo import fields, models


class ExpenseReportWizard(models.TransientModel):
    _name = 'kx.expense.report.wizard'
    _inherit = ['kx.download.mixin']
    _description = 'Expense Report'

    date_from = fields.Date(
        string='From', default=lambda self: fields.Date.context_today(self).replace(day=1))
    date_to = fields.Date(string='To', default=fields.Date.context_today)
    category_ids = fields.Many2many('kx.expense.category', string='Categories')
    hub_ids = fields.Many2many('logistics.hub', string='Hubs')
    include_drafts = fields.Boolean(string='Include drafts')

    def action_export_xlsx(self):
        self.ensure_one()
        states = ('approved', 'draft') if self.include_drafts else ('approved',)
        content = self.env['kx.expense'].expense_report_xlsx(
            self.date_from, self.date_to, self.category_ids.ids or None,
            states, self.hub_ids.ids or None)
        return self._kx_download(content, 'Expenses_%s_%s.xlsx' % (
            self.date_from or 'start', self.date_to or 'today'))
