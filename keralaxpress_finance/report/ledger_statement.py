from odoo import api, models


class LedgerStatementReport(models.AbstractModel):
    _name = 'report.keralaxpress_finance.report_ledger_statement'
    _description = 'Seller Ledger Statement PDF'

    @api.model
    def _get_report_values(self, docids, data=None):
        data = data or {}
        sellers = self.env['logistics.seller'].browse(docids)
        service = self.env['kx.report.service']
        statements = [
            service.ledger_statement(
                seller, data.get('date_from'), data.get('date_to'), data.get('source'))
            for seller in sellers
        ]
        return {
            'doc_ids': docids,
            'doc_model': 'logistics.seller',
            'docs': sellers,
            'statements': statements,
            'company': self.env.company,
        }
